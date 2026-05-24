"""
ArthurOS Services - Semantic Execution Plan
-------------------------------------------
**Fuente única de verdad** para el ejecutor semántico
(``SmartMissionExecutor`` / ``AutoLearningExecutor``).

Antes de este módulo, el executor recibía sus pasos a través de
``semantic_adapter.adapt_mission_to_steps``, que iteraba sobre
``mission.compiled_execution_graph``. Ese grafo está poblado por el
``intent_compiler_v2`` (y por compatibilidad histórica también por
recorders viejos), y arrastra targets legacy:

* ``CLICK`` con ``fallback_strategy=USE_COORDS`` y bbox absoluto.
* Bloques de "click_button", "GroupControl", "guide-service",
  "video-stream" — pintados al colapsar mal el trace.
* ``select_profile`` con ``user_confirmed_label="perfil del navegador"``
  (label genérico que el ApprovalGate aceptaba).

Resultado: el executor "salía como executable" pero acababa intentando
clicks en coordenadas viejas.

Filosofía
~~~~~~~~~

    El usuario expresó una intención (abrir Chrome, buscar canción
    en YouTube). Eso ya está *colapsado* por el
    ``intent_collapse_engine`` en seis pasos canónicos. El executor
    sólo necesita esos seis. Cualquier otro contenido del grafo
    legacy es ruido.

API
~~~

>>> plan = build_semantic_execution_plan(mission)
>>> plan.steps
[SemanticPlanStep(type='open_app', preferred_strategy='open_app:os_startfile', ...),
 SemanticPlanStep(type='select_profile', ...),
 SemanticPlanStep(type='open_new_tab', preferred_strategy='open_new_tab:hotkey_ctrl_t', ...),
 SemanticPlanStep(type='open_url', preferred_strategy='open_url:hotkey_ctrl_l', ...),
 SemanticPlanStep(type='search_youtube', preferred_strategy='search_youtube:dom_input', ...),
 SemanticPlanStep(type='scroll_results', preferred_strategy='scroll_results:wheel', ...)]
>>> plan.coords_used
False
>>> plan.legacy_graph_ignored
True

>>> attach_semantic_plan_to_mission(mission)  # mutación in-place
>>> mission.semantic_execution_plan["source"]
'intent_collapse_engine'

>>> steps = semantic_plan_to_mission_steps(plan)
>>> [s.kind for s in steps]
['open_app', 'select_profile', 'open_new_tab', 'open_url',
 'search_youtube', 'scroll_results']

Reglas duras
~~~~~~~~~~~~

* ``select_profile`` con perfil vacío o con un label genérico
  (``"perfil del navegador"``, ``"el perfil"``, ``"profile"``…) ⇒
  ``needs_user_label=True`` y bloqueante para EXECUTABLE.
* ``open_new_tab`` ⇒ ``preferred_strategy='open_new_tab:hotkey_ctrl_t'``
  fijo. Nunca click_button / vision / coords como principal.
* ``open_url`` con dominio youtube ⇒
  ``preferred_strategy='open_url:hotkey_ctrl_l'`` (Ctrl+L + URL).
* ``search_youtube`` con query + site=youtube ⇒
  ``preferred_strategy='search_youtube:dom_input'`` (DOM-first; UIA/OCR
  como fallback). Nunca coords ni asset visual como primaria.
* ``scroll_results`` ⇒ ``preferred_strategy='scroll_results:wheel'``
  o ``:keyboard``. Nunca coords absolutas.

El plan **filtra** silenciosamente cualquier estrategia prohibida
(``*:visual_asset``, ``*:relative_coords``, ``*:vision_coords``,
``click_button``, ``group_control``, ``guide_service``, ``video_stream``).
Se conservan únicamente como fallback de emergencia y nunca como
primaria. Si todas las estrategias quedan filtradas para un step,
el step entero queda marcado como ``needs_user_label=True``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.contracts.mission import Mission
from app.core.logger import log
from app.services.missions.execution_contracts import MissionStep
from app.services.missions.intent_collapse_engine import (
    CollapsedStep,
    CollapseStatus,
    MissionIntentPlan,
    collapse_intent,
)


# ─────────────────────────────────────────────────────────────────────
# Estrategias prohibidas como PRIMARIA (PRD 2026-05-06c).
# ─────────────────────────────────────────────────────────────────────
#
# El usuario explícitamente prohibió que el SmartExecutor reciba como
# estrategia principal cualquiera de estas — son estrategias "visuales"
# o legacy que el plan semántico nunca debería elegir. Pueden vivir
# como fallback de emergencia, pero nunca primarias.

_FORBIDDEN_PRIMARY_HINTS: Tuple[str, ...] = (
    "vision_coords",
    "relative_coords",
    "visual_asset",
    "click_button",
    "group_control",
    "guide_service",
    "video_stream",
    "use_coords",
)

# ─────────────────────────────────────────────────────────────────────
# Profile labels genéricos que NO sirven como nombre real de perfil.
# ─────────────────────────────────────────────────────────────────────
#
# El ApprovalGate v1 dejaba pasar valores como "perfil del navegador"
# porque no estaba vacío. Para v2 los tratamos como vacíos.

_GENERIC_PROFILE_LABELS: Tuple[str, ...] = (
    "perfil del navegador",
    "perfil del browser",
    "perfil de chrome",
    "perfil de edge",
    "el perfil",
    "perfil",
    "profile",
    "browser profile",
    "chrome profile",
    "default",
    "unknown",
    "user",
    "usuario",
)


def is_generic_profile_label(value: Optional[str]) -> bool:
    """¿``value`` es un label genérico que NO identifica un perfil real?"""
    if not value:
        return True
    norm = " ".join(value.strip().lower().split())
    if not norm:
        return True
    if norm in _GENERIC_PROFILE_LABELS:
        return True
    # Variantes con prefijo común ("usuario default", "perfil x"…).
    if norm.startswith(("perfil ", "el perfil ")) and len(norm) <= 14:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Estrategias canónicas por tipo (única fuente de verdad para
# preferred_strategy y fallback_strategies que recibe el Smart Executor).
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _StrategyPlan:
    preferred: str
    fallbacks: Tuple[str, ...] = ()


_STRATEGY_BY_TYPE: Dict[str, _StrategyPlan] = {
    "open_app": _StrategyPlan(
        preferred="open_app:windows_search",
        fallbacks=("open_app:os_startfile", "open_app:start_command"),
    ),
    # PRD 2026-05-07c §A.7: el plan canónico aprobado debe usar
    # ``select_profile:uia_text_match`` como primaria. ``skip_if_loaded``
    # vive como fallback para el caso "Chrome ya está abierto en el
    # perfil correcto y no hace falta tocar el picker".
    "select_profile": _StrategyPlan(
        preferred="select_profile:uia_text_match",
        fallbacks=(
            "select_profile:visible_text_ocr",
            "select_profile:chrome_profile_alias",
            "select_profile:skip_if_loaded",
        ),
    ),
    # PRD 2026-05-10 §G — Chrome se abrió con un perfil ya activo y
    # NO mostró el picker. La estrategia primaria del executor es
    # "no tocar nada y continuar" (skip_if_loaded). NO inventamos
    # un nombre de perfil.
    "use_selected_profile": _StrategyPlan(
        preferred="select_profile:skip_if_loaded",
        fallbacks=(),
    ),
    "open_new_tab": _StrategyPlan(
        preferred="open_new_tab:hotkey_ctrl_t",
        fallbacks=("open_new_tab:uia_button",),
    ),
    "open_url": _StrategyPlan(
        preferred="open_url:hotkey_ctrl_l",
        fallbacks=(
            "open_url:playwright_goto",
            "open_url:bookmark_uia",
        ),
    ),
    "search_youtube": _StrategyPlan(
        preferred="search_youtube:dom_input",
        fallbacks=(
            "search_youtube:dom_search_input",
            "search_youtube:uia_searchbox",
            "search_youtube:ocr_buscar",
            "search_youtube:keyboard_focus_then_type",
        ),
    ),
    # PRD Nevlan 2026-05-17 — capacidad canónica (provider=…); runtime
    # usa ``search_content:smart_route`` → misma ruta que ``search_site``.
    "search_content": _StrategyPlan(
        preferred="search_content:smart_route",
        fallbacks=(
            "search_site:smart_route",
            "search_site:youtube_dom_input",
            "search_youtube:dom_input",
            "search_youtube:dom_search_input",
            "search_youtube:uia_searchbox",
            "search_youtube:ocr_buscar",
            "search_youtube:keyboard_focus_then_type",
        ),
    ),
    # PRD 2026-05-10 §C — búsqueda en barra del navegador
    # (Google/Bing/DuckDuckGo). Estrategia primaria: enfocar barra
    # con Ctrl+L, escribir, Enter.
    "search_web": _StrategyPlan(
        preferred="search_web:address_bar_type_enter",
        fallbacks=(
            "search_web:dom_search_input",
            "search_web:keyboard_focus_then_type",
        ),
    ),
    # PRD 2026-05-10 §D — abrir resultado de Google/Bing/...
    "open_search_result": _StrategyPlan(
        preferred="open_search_result:dom_link_by_title",
        fallbacks=(
            "open_search_result:uia_text_match",
            "open_search_result:keyboard_tab_then_enter",
        ),
    ),
    "scroll_results": _StrategyPlan(
        preferred="scroll_results:wheel",
        fallbacks=(
            "scroll_results:keyboard",
            "scroll_results:js_scrollby",
        ),
    ),
    # PRD 2026-05-10 §E — scroll en cualquier página del navegador
    # que NO sea YouTube.
    "scroll_page": _StrategyPlan(
        preferred="scroll_page:wheel",
        fallbacks=(
            "scroll_page:keyboard",
            "scroll_page:js_scrollby",
        ),
    ),
    # ── Generic types (Semantic Intent Promotion Engine, 2026-05-14) ──
    # Patrones reutilizables para cualquier sitio/buscador. El
    # ``smart_route`` despacha internamente a la estrategia específica
    # según ``params["site"]`` / ``params["engine"]``.
    "open_site": _StrategyPlan(
        preferred="open_site:smart_route",
        fallbacks=(
            "open_url:hotkey_ctrl_l",
            "open_url:playwright_goto",
            "open_url:bookmark_uia",
        ),
    ),
    "search_site": _StrategyPlan(
        preferred="search_site:smart_route",
        fallbacks=(
            "search_site:youtube_dom_input",
            "search_youtube:dom_input",
            "search_web:address_bar_type_enter",
            "search_web:dom_search_input",
            "search_web:keyboard_focus_then_type",
        ),
    ),
    "web_search": _StrategyPlan(
        preferred="web_search:smart_route",
        fallbacks=(
            "search_web:address_bar_type_enter",
            "search_web:dom_search_input",
            "search_web:keyboard_focus_then_type",
        ),
    ),
    "fill_form": _StrategyPlan(
        preferred="fill_form:uol_fields",
        fallbacks=(
            "fill_field:uol_field",
            "search_web:keyboard_focus_then_type",
        ),
    ),
    "submit_form": _StrategyPlan(
        preferred="submit_form:uol_submit",
        fallbacks=(
            "submit_input:uol_submit",
            "confirm_dialog:dismiss_positive",
        ),
    ),
    "switch_context": _StrategyPlan(
        preferred="switch_context:uol_context",
        fallbacks=("wait_for_state:poll",),
    ),
    "confirm_dialog": _StrategyPlan(
        preferred="confirm_dialog:uol_dialog",
        fallbacks=(
            "submit_input:uol_submit",
            "confirm:ui",
        ),
    ),
    "choose_option": _StrategyPlan(
        preferred="choose_option:list_select",
        fallbacks=(
            "select_visible_option:list_activation",
        ),
    ),
    # UCES — selección primaria entidad-dentro-de-colección (sin coords).
    "select_entity_from_collection": _StrategyPlan(
        preferred="select_entity:collection_first",
        fallbacks=(
            "select_entity:operational_entity_match",
            "select_entity:legacy_kind_fallback",
        ),
    ),
}

# Orden canónico de pasos en una misión browser-search típica.
# El ICE ya emite los steps en este orden, pero re-ordenamos por
# defensa para que el plan sea predecible incluso ante reordenamientos
# accidentales del compiler.
_CANONICAL_ORDER: Tuple[str, ...] = (
    "open_app",
    "select_profile",
    "select_entity_from_collection",
    "use_selected_profile",
    "open_new_tab",
    # Genéricos preferidos en producto (SIPE 2026-05-14). Se ordenan
    # antes de sus equivalentes legacy específicos para que cuando
    # ambos existan (período de transición), el genérico mande.
    "open_site",
    "open_url",
    "search_content",
    "search_site",
    "search_youtube",
    "web_search",
    "search_web",
    "open_search_result",
    "scroll_results",
    "scroll_page",
)


# ─────────────────────────────────────────────────────────────────────
# Modelos del plan
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SemanticPlanStep:
    """Un paso del plan semántico canónico.

    Es independiente de :class:`CompiledStep` legacy (que arrastra
    coords y target_context con bbox). Contiene únicamente:

    * ``id`` estable (hash del intent + posición).
    * ``type`` semántico (``open_app``, ``select_profile``, ...).
    * ``params`` canónicos (sin bbox, sin coords).
    * ``preferred_strategy`` y ``fallback_strategies`` ya curadas.
    * ``confidence`` semántica (0–1).
    * ``human_label`` para vista de revisión.
    * ``needs_user_label`` y ``label_prompt`` cuando el plan no puede
      decidir solo (perfil ambiguo, query vacío...).
    """

    id: str
    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    preferred_strategy: str = ""
    fallback_strategies: List[str] = field(default_factory=list)
    confidence: float = 0.0
    human_label: str = ""
    needs_user_label: bool = False
    label_prompt: str = ""
    block_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "params": dict(self.params),
            "preferred_strategy": self.preferred_strategy,
            "fallback_strategies": list(self.fallback_strategies),
            "confidence": round(float(self.confidence), 3),
            "human_label": self.human_label,
            "needs_user_label": self.needs_user_label,
            "label_prompt": self.label_prompt,
            "block_id": self.block_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SemanticPlanStep":
        return cls(
            id=str(d.get("id") or ""),
            type=str(d.get("type") or ""),
            params=dict(d.get("params") or {}),
            preferred_strategy=str(d.get("preferred_strategy") or ""),
            fallback_strategies=list(d.get("fallback_strategies") or []),
            confidence=float(d.get("confidence") or 0.0),
            human_label=str(d.get("human_label") or ""),
            needs_user_label=bool(d.get("needs_user_label") or False),
            label_prompt=str(d.get("label_prompt") or ""),
            block_id=d.get("block_id"),
        )


@dataclass
class SemanticExecutionPlan:
    """Plan completo que el ``smart_runner_bridge`` consume.

    Garantías:

    * ``coords_used == False`` salvo emergencia explícita (algún step
      no tenía estrategia primaria robusta y se cayó a coords). En
      condiciones normales (la misión browser-search del PRD) debe
      ser siempre ``False``.
    * ``legacy_graph_ignored == True`` cuando el plan se construyó
      ignorando ``compiled_execution_graph`` legacy. Es el camino
      por defecto.
    * ``steps`` está ordenado canónicamente y deduplicado.
    """

    steps: List[SemanticPlanStep] = field(default_factory=list)
    source: str = "intent_collapse_engine"
    version: int = 1
    average_confidence: float = 0.0
    coords_used: bool = False
    legacy_graph_ignored: bool = True
    intent_status: str = CollapseStatus.COMPILED_DRAFT
    # PRD 2026-05-07c §C: razones por las que el plan NO está
    # READY. Lista de códigos como ``EMPTY_PROFILE_NAME_IN_SELECT_PROFILE``,
    # ``QUERY_FIDELITY_SUSPECT``, ``TEMP_ASSET_IN_APPROVED_PLAN``, etc.
    # Vacío ↔ status == READY.
    not_ready_reasons: List[str] = field(default_factory=list)
    # PRD 2026-05-08 (revisión §3): trazabilidad del estado READY.
    #
    # Valores:
    #   * ``"raw_evidence"`` — el ICE extrajo todo desde la grabación
    #     sin necesitar intervención humana. Captura limpia.
    #   * ``"user_confirmation"`` — el plan llegó a READY porque el
    #     usuario confirmó al menos un dato (perfil, query, etc.) en
    #     Mission Review. NO es un estado peor que ``raw_evidence``,
    #     simplemente refleja la realidad: hubo un humano en el loop.
    #   * ``"alias_store"`` — completado automáticamente con datos
    #     que el sistema aprendió de confirmaciones anteriores.
    #   * ``"unknown"`` — no se pudo determinar (típicamente porque
    #     el plan no está READY todavía).
    #
    # En vista normal de Mission Review, este campo es ignorado: el
    # usuario solo ve "Lista para ejecutar". En modo experto se
    # expone como "READY por confirmación humana, no por captura
    # perfecta" para auditoría.
    ready_provenance: str = "unknown"

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def needs_user_label(self) -> bool:
        return any(s.needs_user_label for s in self.steps)

    @property
    def is_executable(self) -> bool:
        return (
            bool(self.steps)
            and not self.needs_user_label
            and self.intent_status == CollapseStatus.EXECUTABLE
        )

    @property
    def kinds(self) -> List[str]:
        return [s.type for s in self.steps]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "average_confidence": round(float(self.average_confidence), 3),
            "coords_used": bool(self.coords_used),
            "legacy_graph_ignored": bool(self.legacy_graph_ignored),
            "intent_status": self.intent_status,
            "not_ready_reasons": list(self.not_ready_reasons),
            "ready_provenance": str(self.ready_provenance or "unknown"),
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SemanticExecutionPlan":
        return cls(
            steps=[SemanticPlanStep.from_dict(x)
                   for x in (d.get("steps") or [])],
            source=str(d.get("source") or "intent_collapse_engine"),
            version=int(d.get("version") or 1),
            average_confidence=float(d.get("average_confidence") or 0.0),
            coords_used=bool(d.get("coords_used") or False),
            legacy_graph_ignored=bool(d.get("legacy_graph_ignored") or True),
            intent_status=str(d.get("intent_status") or CollapseStatus.COMPILED_DRAFT),
            not_ready_reasons=list(d.get("not_ready_reasons") or []),
            ready_provenance=str(d.get("ready_provenance") or "unknown"),
        )


# ─────────────────────────────────────────────────────────────────────
# Construcción del plan
# ─────────────────────────────────────────────────────────────────────


def _strategies_for(step_type: str) -> _StrategyPlan:
    return _STRATEGY_BY_TYPE.get(step_type, _StrategyPlan(preferred="unknown"))


def _strip_forbidden(strategies: Iterable[str]) -> Tuple[List[str], bool]:
    """Devuelve ``(strategies_clean, has_forbidden)``.

    Mantiene el orden y elimina duplicados.
    """
    out: List[str] = []
    forbidden = False
    seen = set()
    for s in strategies:
        if not s:
            continue
        norm = s.strip()
        if not norm or norm in seen:
            continue
        if any(h in norm.lower() for h in _FORBIDDEN_PRIMARY_HINTS):
            forbidden = True
            continue
        out.append(norm)
        seen.add(norm)
    return out, forbidden


def _params_for_open_app(cs: CollapsedStep) -> Dict[str, Any]:
    name = (cs.params.get("app") or cs.params.get("name") or "").strip()
    method = (cs.params.get("method") or "windows_search").strip()
    return {"name": name or "chrome", "method": method}


def _params_for_select_profile(
    cs: CollapsedStep,
) -> Tuple[Dict[str, Any], bool, str]:
    """Devuelve ``(params, needs_user_label, prompt)``."""
    raw_profile = (cs.params.get("profile") or cs.params.get("profile_name") or "").strip()
    needs_label = bool(cs.needs_user_label) or is_generic_profile_label(raw_profile)
    prompt = cs.label_prompt or "¿Qué perfil seleccionaste?"
    profile = "" if needs_label else raw_profile
    params = {
        "profile_name": profile,
        "app": (cs.params.get("app") or "chrome").strip() or "chrome",
    }
    return params, needs_label, prompt


def _params_for_open_new_tab(cs: CollapsedStep) -> Dict[str, Any]:
    # Solo metadatos; el ``preferred_strategy`` canonical lo añade
    # ``semantic_plan_to_mission_steps`` desde ``sp.preferred_strategy``.
    return {
        "app": (cs.params.get("app") or "chrome").strip() or "chrome",
    }


_KNOWN_URL_ALIASES = {
    "youtube": "youtube",
    "youtube.com": "youtube",
    "google": "google",
    "google.com": "google",
    "gmail": "gmail",
    "drive": "drive",
    "github": "github",
}


def _params_for_open_url(cs: CollapsedStep) -> Tuple[Dict[str, Any], bool, str]:
    """Devuelve ``(params, needs_user_label, prompt)``."""
    url = (cs.params.get("url") or "").strip()
    name = (cs.params.get("name") or cs.params.get("alias") or "").strip()
    if not url and not name:
        return (
            {},
            True,
            "¿Qué URL querías abrir? (ej. youtube.com)",
        )
    params: Dict[str, Any] = {}
    if url:
        params["url"] = url
    # Resolución de alias canonical en minúsculas. Si el url o el name
    # contiene un dominio conocido, fijamos el alias canónico.
    blob = f"{url} {name}".lower()
    canonical_alias = ""
    for hint, canon in _KNOWN_URL_ALIASES.items():
        if hint in blob:
            canonical_alias = canon
            break
    if canonical_alias:
        params["alias"] = canonical_alias
    elif name:
        # Sin coincidencia canónica → usamos el name normalizado.
        params["alias"] = name.lower()
    return params, False, ""


def _params_for_search_youtube(
    cs: CollapsedStep,
) -> Tuple[Dict[str, Any], bool, str]:
    query = (cs.params.get("query") or cs.params.get("val") or "").strip()
    if not query:
        return (
            {},
            True,
            "¿Qué querías buscar en YouTube?",
        )
    # PRD 2026-05-07c §B: arrastrar ``query_fidelity_status`` para que
    # ``compute_ready_status`` pueda detectar pérdida de tokens cortos
    # ("de", "la"…) sin re-ejecutar el assessor.
    fidelity = (cs.params.get("query_fidelity_status") or "ok")
    params: Dict[str, Any] = {
        "query": query,
        "site": "youtube",
        "target": "youtube_search_box",
        "query_fidelity_status": fidelity,
    }
    # PRD 2026-05-08: si el ICE marcó suspect_pattern (heurística
    # de conector faltante) propagamos también la sugerencia para
    # que Mission Review pueda preseleccionarla.
    if cs.params.get("suggested_query"):
        params["suggested_query"] = cs.params["suggested_query"]
    if cs.params.get("suspected_missing_connector"):
        params["suspected_missing_connector"] = (
            cs.params["suspected_missing_connector"]
        )
    needs_label = False
    prompt = ""
    if fidelity in ("missing_short_token", "suspect"):
        needs_label = True
        prompt = (
            cs.label_prompt
            or f"¿La búsqueda correcta es \"{query}\"?"
        )
    return params, needs_label, prompt


def _params_for_scroll_results(cs: CollapsedStep) -> Dict[str, Any]:
    direction = (cs.params.get("direction") or "down").strip().lower() or "down"
    try:
        amount = max(1, int(cs.params.get("amount") or 2))
    except Exception:
        amount = 2
    context = (cs.params.get("context") or "youtube_results").strip()
    return {"direction": direction, "amount": amount, "context": context}


def _params_for_use_selected_profile(cs: CollapsedStep) -> Dict[str, Any]:
    """PRD 2026-05-10 §G: el ejecutor recibe únicamente la app y el
    flag de resolución. NO incluye ``profile_name`` (jamás
    inventamos un nombre).
    """
    return {
        "app": (cs.params.get("app") or "chrome").strip() or "chrome",
        "profile_resolution": (
            cs.params.get("profile_resolution")
            or "already_active_or_selected"
        ),
    }


def _params_for_search_web(
    cs: CollapsedStep,
) -> Tuple[Dict[str, Any], bool, str]:
    query = (cs.params.get("query") or "").strip()
    if not query:
        return ({}, True, "¿Qué querías buscar en el navegador?")
    engine = (cs.params.get("engine") or "google").strip().lower() or "google"
    target = (cs.params.get("target") or "browser_address_bar").strip()
    return (
        {
            "query": query,
            "engine": engine,
            "target": target,
            "submit": True,
        },
        False,
        "",
    )


def _params_for_open_search_result(
    cs: CollapsedStep,
) -> Tuple[Dict[str, Any], bool, str]:
    title = (cs.params.get("title") or "").strip()
    if not title:
        return (
            {},
            True,
            "¿Qué resultado debías abrir?",
        )
    return (
        {
            "title": title,
            "query": (cs.params.get("query") or "").strip(),
            "result_context": (
                cs.params.get("result_context") or "google_results"
            ).strip(),
        },
        False,
        "",
    )


def _params_for_scroll_page(cs: CollapsedStep) -> Dict[str, Any]:
    direction = (cs.params.get("direction") or "down").strip().lower() or "down"
    try:
        amount = max(1, int(cs.params.get("amount") or 1))
    except Exception:
        amount = 1
    context = (cs.params.get("context") or "browser_page").strip()
    return {"direction": direction, "amount": amount, "context": context}


def _convert_collapsed_step(
    cs: CollapsedStep,
    *,
    index: int,
    mission_id: str,
) -> Optional[SemanticPlanStep]:
    """Traduce un :class:`CollapsedStep` al formato canónico del plan."""
    t = cs.type
    needs_label = False
    prompt = ""

    if t == "open_app":
        params = _params_for_open_app(cs)
    elif t == "select_profile":
        params, needs_label, prompt = _params_for_select_profile(cs)
    elif t == "use_selected_profile":
        params = _params_for_use_selected_profile(cs)
    elif t == "open_new_tab":
        params = _params_for_open_new_tab(cs)
    elif t == "open_url":
        params, needs_label, prompt = _params_for_open_url(cs)
    elif t == "search_youtube":
        params, needs_label, prompt = _params_for_search_youtube(cs)
        # UIC canónico (2026-05-17): ``search_content(provider=youtube)``.
        params.setdefault("provider", "youtube")
        params.setdefault("site", params.get("site") or "youtube")
        t = "search_content"
    elif t == "search_web":
        params, needs_label, prompt = _params_for_search_web(cs)
    elif t == "open_search_result":
        params, needs_label, prompt = _params_for_open_search_result(cs)
    elif t == "scroll_results":
        params = _params_for_scroll_results(cs)
    elif t == "scroll_page":
        params = _params_for_scroll_page(cs)
    else:
        # Tipo no soportado por el plan → se descarta.
        return None

    sid = f"{mission_id}:p{index:02d}:{t}"
    plan = _strategies_for(t)
    fb_clean, _ = _strip_forbidden(plan.fallbacks)

    # PRD 2026-05-07c: si el ICE ya marcó el step como
    # ``needs_user_label=True`` (caso típico: query fidelity sospechosa,
    # perfil sin canonical), heredamos el flag en el plan canónico —
    # NO se silencia. El conversor solo puede AÑADIR razones para
    # confirmar; no quitar las que el ICE detectó.
    if cs.needs_user_label and not needs_label:
        needs_label = True
        prompt = prompt or cs.label_prompt or (
            "Necesito tu confirmación para este paso."
        )

    # La estrategia primaria nunca puede estar prohibida; usamos
    # _STRATEGY_BY_TYPE como única fuente.
    primary = plan.preferred
    if any(h in primary.lower() for h in _FORBIDDEN_PRIMARY_HINTS):
        # Defensivo: alguien añadió una estrategia inválida.
        log.warning(
            f"[semantic_plan] estrategia primaria prohibida para "
            f"{t}: {primary}; degradando a 'unknown'."
        )
        primary = "unknown"
        needs_label = True
        prompt = prompt or f"¿Cómo quieres ejecutar {t}?"

    if needs_label:
        # Aún producimos el step (con label_prompt), pero el bridge lo
        # tratará como bloqueante para EXECUTABLE.
        prompt = prompt or "Necesito tu confirmación para este paso."

    return SemanticPlanStep(
        id=sid,
        type=t,
        params=params,
        preferred_strategy=primary,
        fallback_strategies=fb_clean,
        confidence=float(cs.confidence or 0.0),
        human_label=cs.human_label or t,
        needs_user_label=needs_label,
        label_prompt=prompt if needs_label else "",
        block_id=None,
    )


def _dedup_and_order(
    raw: List[SemanticPlanStep],
) -> List[SemanticPlanStep]:
    """Dedup por ``type`` (manteniendo el primero) y orden canónico."""
    by_type: Dict[str, SemanticPlanStep] = {}
    for s in raw:
        if s.type not in by_type:
            by_type[s.type] = s
    ordered: List[SemanticPlanStep] = []
    for kind in _CANONICAL_ORDER:
        if kind in by_type:
            ordered.append(by_type[kind])
    # Por si quedó algún tipo nuevo que no esté en el orden canónico.
    for t, step in by_type.items():
        if t not in _CANONICAL_ORDER:
            ordered.append(step)
    return ordered


def build_semantic_execution_plan(
    mission: Mission,
    *,
    intent_plan: Optional[MissionIntentPlan] = None,
    use_existing: bool = True,
) -> SemanticExecutionPlan:
    """Construye un :class:`SemanticExecutionPlan` para la misión.

    Args:
        mission: misión a ejecutar (debe tener ``raw_trace`` o
            ``compiled_execution_graph`` para que el ICE pueda colapsar).
        intent_plan: plan ya colapsado (lo recibe de tests / pipelines
            que ya corrieron ``collapse_intent``). Si se pasa explícito,
            siempre se usa para construir el plan (ignora cualquier
            plan persistido).
        use_existing: si ``True`` (default) **y** ``intent_plan`` es
            ``None`` **y** la misión ya trae
            ``mission.semantic_execution_plan``, se reusa SIN re-correr
            el ICE. Pasar ``False`` para forzar reconstrucción
            (útil tras editar la misión en Mission Review).

    Returns:
        Plan canónico listo para ``smart_runner_bridge``.
    """
    # Si el caller forzó un intent_plan, manda él: no leemos lo
    # persistido (sería caching estancado).
    if intent_plan is None and use_existing and mission.semantic_execution_plan:
        try:
            return SemanticExecutionPlan.from_dict(mission.semantic_execution_plan)
        except Exception as e:
            log.debug(f"[semantic_plan] reuse falló: {e}; reconstruyendo")

    if intent_plan is None:
        try:
            intent_plan = collapse_intent(mission=mission)
        except Exception as e:
            log.exception(f"[semantic_plan] collapse_intent falló: {e}")
            intent_plan = MissionIntentPlan()

    mid = (mission.id or "m").strip() or "m"

    raw_steps: List[SemanticPlanStep] = []
    for i, cs in enumerate(intent_plan.semantic_steps or []):
        sp = _convert_collapsed_step(cs, index=i, mission_id=mid)
        if sp is not None:
            raw_steps.append(sp)

    steps = _dedup_and_order(raw_steps)

    # Confianza promedio (sólo de pasos no-bloqueados; los needs_label
    # entran con su confianza pero no penalizamos la media).
    if steps:
        avg = sum(float(s.confidence) for s in steps) / float(len(steps))
    else:
        avg = 0.0

    plan = SemanticExecutionPlan(
        steps=steps,
        source="intent_collapse_engine",
        version=1,
        average_confidence=round(float(avg), 4),
        coords_used=False,         # primaria nunca usa coords
        legacy_graph_ignored=True,  # construimos sólo desde ICE
        intent_status=intent_plan.status,
    )
    return plan


def attach_semantic_plan_to_mission(
    mission: Mission,
    *,
    plan: Optional[SemanticExecutionPlan] = None,
    force: bool = False,
) -> SemanticExecutionPlan:
    """Construye el plan (si no se pasó) y lo persiste en la misión.

    Devuelve el plan resultante. Mutación in-place de
    ``mission.semantic_execution_plan``. Antes de persistir, recalcula
    ``intent_status`` aplicando :func:`compute_ready_status` (PRD
    2026-05-07c §C): el plan queda en ``READY`` si y solo si TODOS
    los invariantes de producto se cumplen.
    """
    if plan is None:
        plan = build_semantic_execution_plan(mission, use_existing=not force)
    # Persistir provisionalmente (sin status final) para que
    # compute_ready_status pueda inspeccionar /temp/ y demás dentro
    # del blob de la misión.
    mission.semantic_execution_plan = plan.to_dict()
    attach_intent_status_to_plan(plan, mission=mission)
    mission.semantic_execution_plan = plan.to_dict()
    try:
        from app.services.missions.four_layer_operational_model import (
            sync_four_layer_after_sep_update,
        )

        sync_four_layer_after_sep_update(mission)
    except Exception as exc:
        log.debug("[sep] four-layer sync: %s", exc)
    return plan


# ─────────────────────────────────────────────────────────────────────
# Conversión a MissionStep[] para el Smart Executor
# ─────────────────────────────────────────────────────────────────────


def _executor_kind_for_semantic_step(sp: SemanticPlanStep) -> str:
    """``MissionStep.kind`` debe alinearse con el tipo SEP (UIC persistido).

    El routing YouTube/site vive en ``ActionRunner`` (``*:smart_route``,
    ``search_site:youtube_dom_input``, …).  ``search_youtube`` queda sólo
    como alias legacy explícito en pasos antiguos que ya traían ese kind.
    """
    return sp.type


def semantic_plan_to_mission_steps(
    plan: SemanticExecutionPlan,
) -> List[MissionStep]:
    """Convierte el plan en la lista plana de :class:`MissionStep`.

    Saltamos los steps que requieren confirmación humana
    (``needs_user_label=True``): el bridge debe haberlos detectado
    antes y bloqueado la ejecución (esa es la regla EXECUTABLE).
    Esta función no es responsable del gating, sólo de la conversión.

    Sin embargo, sí filtramos cualquier ``params`` que apeste a coords
    legacy (``bbox``, ``coords_xy``, ``fallback_coords``) — el plan
    canónico no debe arrastrarlas y el SmartExecutor debe rechazarlas.
    """
    out: List[MissionStep] = []
    for sp in plan.steps:
        if sp.params.get("disabled") or sp.params.get("four_layer_disabled"):
            continue
        # Cleanup defensivo de params: nada de coords absolutas y
        # nada de claves reservadas para que el SmartExecutor las pise.
        forbidden_keys = {
            "bbox", "coords_xy", "fallback_coords",
            "absolute_xy", "click_offset_within_bbox",
            # también los campos sintéticos del PRD que pueden colisionar
            # con la canonical strategy resuelta abajo:
            "preferred_strategy", "fallback_strategies",
        }
        clean_params = {
            k: v for k, v in sp.params.items()
            if k not in forbidden_keys
        }
        # Inyectamos la estrategia CANONICAL del plan (resuelta por
        # ``_STRATEGY_BY_TYPE``). Es lo que el SmartExecutor lee en
        # ``_resolve_strategies`` para forzar la primaria.
        clean_params["preferred_strategy"] = sp.preferred_strategy
        if sp.fallback_strategies:
            clean_params["fallback_strategies"] = list(sp.fallback_strategies)
        eff_kind = _executor_kind_for_semantic_step(sp)
        out.append(MissionStep(
            id=sp.id,
            kind=eff_kind,
            params=clean_params,
            block_id=sp.block_id,
            human_label=sp.human_label,
        ))
    return out


# ─────────────────────────────────────────────────────────────────────
# Helpers públicos
# ─────────────────────────────────────────────────────────────────────


def has_semantic_plan(mission: Mission) -> bool:
    """¿La misión tiene un plan semántico utilizable?

    Versión "blanda": basta con que haya al menos un step en el blob.
    Para validación semántica robusta usar
    :func:`has_valid_semantic_execution_plan` o
    :func:`validate_semantic_execution_plan_strict`.
    """
    blob = mission.semantic_execution_plan
    if not blob:
        return False
    steps = blob.get("steps") or []
    return bool(steps)


# ─────────────────────────────────────────────────────────────────────
# Whitelist semántica (PRD 2026-05-07).
#
# Fuente única de verdad para "¿este step del plan representa una
# intención semántica reconocible?". Derivada del catálogo canónico
# ``_STRATEGY_BY_TYPE`` para que añadir un tipo nuevo ahí lo habilite
# automáticamente en validadores, ApprovalGate, smart_runner_bridge,
# Ghost Simulation y SmartExecutor sin tocar código en cada uno.
#
# Si necesitas extender el set sin tocar el catálogo de estrategias
# (ej. ``"wait_for"``, ``"confirm"``), añádelo a ``_EXTRA_SEMANTIC_TYPES``
# de abajo.
# ─────────────────────────────────────────────────────────────────────

_EXTRA_SEMANTIC_TYPES: frozenset = frozenset({
    # Intenciones reconocibles que aún no tienen entry en
    # ``_STRATEGY_BY_TYPE`` pero que el ICE / Mission Review pueden
    # emitir como pasos válidos.
    "wait_for",
    "confirm",
})

SEMANTIC_TYPES: frozenset = frozenset(_STRATEGY_BY_TYPE.keys()) | _EXTRA_SEMANTIC_TYPES


# Llaves "ejecutables" que el SmartExecutor consume del step. Cualquier
# valor legacy en alguna de ellas contamina el step entero.
_EXECUTABLE_FIELDS: Tuple[str, ...] = ("type", "intent", "action")


def _normalize_field_value(raw: Any) -> Optional[str]:
    """Normaliza un valor de ``type``/``intent``/``action`` para
    comparar contra la blacklist o la whitelist.

    * ``None`` o cadena vacía → ``None`` (no hay valor para comparar).
    * Cualquier ``str`` → ``str.strip().lower()``; si tras normalizar
      queda vacío (era solo whitespace) también devuelve ``None``.
    * Cualquier otro tipo (``list``/``dict``/``int``…) → ``None`` (lo
      trata como "no comparable"; el caller debe haber validado el
      tipo antes con :func:`_is_acceptable_executable_field_type`).
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        norm = raw.strip().lower()
        return norm or None
    return None


def _is_acceptable_executable_field_type(raw: Any) -> bool:
    """Las llaves ejecutables solo aceptan ``str`` (o estar ausentes).

    Si el valor es ``list``, ``dict``, ``int``, etc., el step está
    malformado y se reporta como
    ``INVALID_EXECUTABLE_FIELD_IN_SEMANTIC_PLAN``.
    """
    return raw is None or isinstance(raw, str)


def _has_semantic_step_shape(step: Any) -> bool:
    """¿Este step del plan tiene forma semántica reconocible?

    Acepta el step si:
      * tiene ``intent`` o ``action`` con un valor **string no
        vacío** (después de normalizar — strings de solo whitespace
        no cuentan), o
      * tiene ``type`` cuyo valor normalizado (``strip().lower()``)
        está en la whitelist :data:`SEMANTIC_TYPES`.

    Rechaza ``{"type": "click"}``, ``{"type": "mouse_scroll"}`` y
    cualquier otro residuo legacy. También rechaza variantes como
    ``{"intent": "   "}`` o ``{"action": ""}`` que aparentaban ser
    semánticas pero quedaron vacías.

    Esta es la versión "tolerante" usada por
    :func:`has_valid_semantic_execution_plan` (routing). La versión
    estricta de approval también valida canonicidad de ``type``
    (``"OPEN_APP"`` no se acepta silenciosamente como ``open_app``).
    """
    if not isinstance(step, dict):
        return False
    if _normalize_field_value(step.get("intent")) is not None:
        return True
    if _normalize_field_value(step.get("action")) is not None:
        return True
    step_type_norm = _normalize_field_value(step.get("type"))
    return step_type_norm is not None and step_type_norm in SEMANTIC_TYPES


def has_valid_semantic_execution_plan(mission: Any) -> bool:
    """**Routing-level check.** Devuelve ``True`` si existe al menos
    un step semántico reconocible dentro del ``semantic_execution_plan``.

    Esta función decide si la misión entra a la **ruta semántica** del
    runner bridge / approval gate. NO garantiza que TODO el plan esté
    limpio — un plan con un step semántico válido + cinco residuos
    legacy seguirá pasando este check.

    Para garantía estricta de calidad (todos los steps son semánticos,
    sin ``click``/``mouse_scroll``/``key_press``/etc) usar
    :func:`validate_semantic_execution_plan_strict`. El ApprovalGate y
    el SmartExecutor combinan ambas:

    1. ``has_valid_semantic_execution_plan`` → ¿entro a ruta semántica?
    2. ``validate_semantic_execution_plan_strict`` → ¿el plan es
       ejecutable sin contaminación legacy?

    Ese contrato evita que cinco lugares distintos (adapter, gate,
    bridge, ghost, executor) acaben aplicando criterios divergentes.
    """
    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        return False
    steps = sep.get("steps")
    if not isinstance(steps, list):
        return False
    return any(_has_semantic_step_shape(step) for step in steps)


# ─────────────────────────────────────────────────────────────────────
# Validación estricta del plan (PRD 2026-05-07 follow-up).
#
# "La evidencia puede ser ruidosa. La intención ejecutable debe ser
# limpia."  El ``compiled_execution_graph`` (legacy/debug) puede tener
# clicks crudos, GroupControl, etc — eso es evidencia de captura. Pero
# el ``semantic_execution_plan`` que el SmartExecutor consume NO puede
# arrastrar ningún step legacy adentro.
# ─────────────────────────────────────────────────────────────────────


# Tipos que NUNCA deben aparecer dentro del semantic_execution_plan.
# Si alguno aparece, es evidencia de que el ICE se contaminó (o de que
# alguien escribió a mano un blob crudo en el plan).
#
# La blacklist está SIEMPRE en formato normalizado (lowercase, sin
# espacios) — la comparación contra cada step pasa por
# ``_normalize_field_value`` para que variantes como ``"Click"``,
# ``"  click  "`` o ``"MOUSE_SCROLL"`` se detecten igual y nadie pueda
# colar legacy aprovechando el casing.
_LEGACY_STEP_TYPES_BLACKLIST: frozenset = frozenset({
    "click",
    "click_button",
    "double_click",
    "right_click",
    "mouse_click",
    "mouse_scroll",
    "scroll",
    "key_press",
    "key_press_enter",
    "type",
    "type_text",
    "send_hotkey",
    "coordinate_click",
    "use_coords",
    "groupcontrol",
    "guide-service",
    "guide_service",
    "video-stream",
    "video_stream",
    # Strategies-as-types (cuando alguien volcó la strategy en el
    # campo type).
    "vision_coords",
    "relative_coords",
    "visual_asset",
})

# NOTA: ``_EXECUTABLE_FIELDS``, ``_normalize_field_value`` y
# ``_is_acceptable_executable_field_type`` se definen arriba (cerca de
# ``SEMANTIC_TYPES``) porque ``_has_semantic_step_shape`` también los
# usa.


# Códigos de violación emitidos por la validación estricta. Estos
# nombres se exportan para que ApprovalGate, SmartExecutor y los tests
# puedan consultarlos sin acoplamiento por strings sueltos.
class StrictValidationCode:
    EMPTY_PLAN = "EMPTY_SEMANTIC_PLAN"
    STEPS_NOT_LIST = "SEMANTIC_PLAN_STEPS_NOT_LIST"
    STEP_NOT_DICT = "STEP_NOT_DICT_IN_SEMANTIC_PLAN"
    EMPTY_STEP = "EMPTY_STEP_IN_SEMANTIC_PLAN"
    LEGACY_STEP = "LEGACY_STEP_INSIDE_SEMANTIC_PLAN"
    NON_SEMANTIC_STEP = "NON_SEMANTIC_STEP_IN_SEMANTIC_PLAN"
    INVALID_EXECUTABLE_FIELD = "INVALID_EXECUTABLE_FIELD_IN_SEMANTIC_PLAN"
    NON_CANONICAL_TYPE = "NON_CANONICAL_TYPE_IN_SEMANTIC_PLAN"
    UNKNOWN_UIC_CAPABILITY = "UNKNOWN_UIC_CAPABILITY_IN_SEMANTIC_PLAN"


# PRD 2026-05-07c §C: códigos READY-blocker. Cuando alguno aparece
# en ``not_ready_reasons``, el plan NO puede pasar a READY (se queda
# en ``NEEDS_REVIEW``). Son específicos para que la UI/Mission
# Review pueda mostrar la confirmación mínima correspondiente y
# guardar la respuesta como aprendizaje persistente.
class ReadyBlockerCode:
    EMPTY_PROFILE_NAME_IN_SELECT_PROFILE = "EMPTY_PROFILE_NAME_IN_SELECT_PROFILE"
    QUERY_FIDELITY_SUSPECT = "QUERY_FIDELITY_SUSPECT"
    QUERY_MISSING_SHORT_TOKEN = "QUERY_MISSING_SHORT_TOKEN"
    SEMANTIC_INTENT_UNDER_SPECIFIED = "SEMANTIC_INTENT_UNDER_SPECIFIED"
    SEMANTIC_INTENT_MULTI_TARGET = "SEMANTIC_INTENT_MULTI_TARGET"
    PROFILE_RESOLUTION_REQUIRED = "PROFILE_RESOLUTION_REQUIRED"
    SEMANTIC_PLAN_NOT_READY = "SEMANTIC_PLAN_NOT_READY"
    TEMP_ASSET_IN_APPROVED_PLAN = "TEMP_ASSET_IN_APPROVED_PLAN"
    LEGACY_RUNTIME_CONTAMINATION = "LEGACY_RUNTIME_CONTAMINATION"
    EMPTY_QUERY_IN_SEARCH = "EMPTY_QUERY_IN_SEARCH"
    EMPTY_URL_IN_OPEN_URL = "EMPTY_URL_IN_OPEN_URL"
    EMPTY_APP_IN_OPEN_APP = "EMPTY_APP_IN_OPEN_APP"
    NEEDS_USER_LABEL_PENDING = "NEEDS_USER_LABEL_PENDING"
    # PRD 2026-05-10 §A — eventos relevantes del raw_trace que el
    # engine NO logró representar en ningún step semántico ni
    # marcar como noise. El plan no puede pasar a READY mientras
    # haya al menos uno; los detalles (event_id, event_type, reason,
    # suggested_semantic_type) viven en
    # ``mission._dropped_semantic_events``.
    SEMANTIC_EVENT_DROPPED_FROM_PLAN = "SEMANTIC_EVENT_DROPPED_FROM_PLAN"
    # UCES — compilación primaria entidad+colección
    COLLECTION_ENTITY_NEEDS_CLARIFICATION = "COLLECTION_ENTITY_NEEDS_CLARIFICATION"
    COLLECTION_ENTITY_WEAK = "COLLECTION_ENTITY_WEAK"
    CANONICAL_TRUTH_NOT_MATERIALIZED = "CANONICAL_TRUTH_NOT_MATERIALIZED"


# Conjunto de todos los READY-blocker codes para que el ApprovalGate
# y el SmartRunner Bridge los reconozcan sin re-importar la clase.
READY_BLOCKER_CODES: frozenset = frozenset({
    ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE,
    ReadyBlockerCode.QUERY_FIDELITY_SUSPECT,
    ReadyBlockerCode.QUERY_MISSING_SHORT_TOKEN,
    ReadyBlockerCode.SEMANTIC_INTENT_UNDER_SPECIFIED,
    ReadyBlockerCode.SEMANTIC_INTENT_MULTI_TARGET,
    ReadyBlockerCode.PROFILE_RESOLUTION_REQUIRED,
    ReadyBlockerCode.SEMANTIC_PLAN_NOT_READY,
    ReadyBlockerCode.TEMP_ASSET_IN_APPROVED_PLAN,
    ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION,
    ReadyBlockerCode.EMPTY_QUERY_IN_SEARCH,
    ReadyBlockerCode.EMPTY_URL_IN_OPEN_URL,
    ReadyBlockerCode.EMPTY_APP_IN_OPEN_APP,
    ReadyBlockerCode.NEEDS_USER_LABEL_PENDING,
    ReadyBlockerCode.SEMANTIC_EVENT_DROPPED_FROM_PLAN,
    ReadyBlockerCode.COLLECTION_ENTITY_NEEDS_CLARIFICATION,
    ReadyBlockerCode.COLLECTION_ENTITY_WEAK,
})

# Solo perfil / query pueden quedar pendientes cuando el pipeline
# canónico está bien (PRD SIPE §2026-05-15 — misión tipo YouTube /
# Chrome). Otros motivos ⇒ fallo contrato integral.
PROFILE_OR_QUERY_REVIEW_FOCUS_BLOCKERS: frozenset = frozenset({
    ReadyBlockerCode.EMPTY_PROFILE_NAME_IN_SELECT_PROFILE,
    ReadyBlockerCode.QUERY_FIDELITY_SUSPECT,
    ReadyBlockerCode.QUERY_MISSING_SHORT_TOKEN,
    ReadyBlockerCode.PROFILE_RESOLUTION_REQUIRED,
    ReadyBlockerCode.EMPTY_QUERY_IN_SEARCH,
    ReadyBlockerCode.NEEDS_USER_LABEL_PENDING,
    ReadyBlockerCode.SEMANTIC_INTENT_UNDER_SPECIFIED,
    ReadyBlockerCode.SEMANTIC_INTENT_MULTI_TARGET,
})


@dataclass
class SemanticPlanViolation:
    """Una violación encontrada por la validación estricta."""

    code: str
    step_index: int
    step_repr: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "step_index": self.step_index,
            "step_repr": self.step_repr,
            "message": self.message,
        }


@dataclass
class SemanticPlanValidation:
    """Resultado de la validación estricta del plan.

    ``is_valid`` solo es ``True`` cuando NO hay violaciones — es decir,
    el plan está completamente limpio y listo para ejecución.
    """

    is_valid: bool = True
    violations: List[SemanticPlanViolation] = field(default_factory=list)

    @property
    def violation_codes(self) -> List[str]:
        return [v.code for v in self.violations]

    def add(
        self,
        code: str,
        *,
        step_index: int = -1,
        step_repr: str = "",
        message: str = "",
    ) -> None:
        self.violations.append(SemanticPlanViolation(
            code=code,
            step_index=step_index,
            step_repr=step_repr,
            message=message,
        ))
        self.is_valid = False


def _short_step_repr(step: Any, *, max_len: int = 80) -> str:
    """Repr corto y seguro para logs/UX."""
    try:
        s = repr(step)
    except Exception:
        s = "<unrepresentable>"
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def validate_semantic_execution_plan_strict(
    mission: Any,
) -> SemanticPlanValidation:
    """**Approval/execution-level check.** Garantiza que todos los
    steps ejecutables dentro del ``semantic_execution_plan`` sean
    semánticos y que no haya residuos legacy.

    Reglas (códigos en :class:`StrictValidationCode`):

    * ``EMPTY_SEMANTIC_PLAN`` — no hay plan o no tiene steps.
    * ``SEMANTIC_PLAN_STEPS_NOT_LIST`` — ``steps`` no es lista.
    * ``STEP_NOT_DICT_IN_SEMANTIC_PLAN`` — un elemento no es dict
      (ej. ``"click basura"`` colado).
    * ``EMPTY_STEP_IN_SEMANTIC_PLAN`` — un step es ``{}``.
    * ``LEGACY_STEP_INSIDE_SEMANTIC_PLAN`` — el ``type`` figura en la
      blacklist legacy (``click``, ``mouse_scroll``, ``key_press``,
      ``type_text``, ``coordinate_click``, ``use_coords``,
      ``GroupControl``, ``guide-service``…).
    * ``NON_SEMANTIC_STEP_IN_SEMANTIC_PLAN`` — el step no tiene
      ``intent`` ni ``action`` ni un ``type`` en
      :data:`SEMANTIC_TYPES`.

    Esta función NO mira el ``compiled_execution_graph`` — la basura
    legacy ahí es evidencia válida de captura, no afecta la decisión
    de ejecución.
    """
    result = SemanticPlanValidation()

    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        result.add(
            StrictValidationCode.EMPTY_PLAN,
            message="La misión no tiene semantic_execution_plan.",
        )
        return result

    steps = sep.get("steps")
    if steps is None:
        result.add(
            StrictValidationCode.EMPTY_PLAN,
            message="semantic_execution_plan.steps ausente.",
        )
        return result
    if not isinstance(steps, list):
        result.add(
            StrictValidationCode.STEPS_NOT_LIST,
            message=(
                f"semantic_execution_plan.steps debe ser lista; "
                f"recibido {type(steps).__name__}."
            ),
        )
        return result
    if len(steps) == 0:
        result.add(
            StrictValidationCode.EMPTY_PLAN,
            message="semantic_execution_plan.steps está vacío.",
        )
        return result

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            result.add(
                StrictValidationCode.STEP_NOT_DICT,
                step_index=idx,
                step_repr=_short_step_repr(step),
                message=(
                    f"Step #{idx} no es dict: {_short_step_repr(step)}"
                ),
            )
            continue

        if not step:
            result.add(
                StrictValidationCode.EMPTY_STEP,
                step_index=idx,
                step_repr="{}",
                message=f"Step #{idx} es un dict vacío.",
            )
            continue

        # ¿alguna llave ejecutable (type / intent / action) tiene un
        # tipo inválido (no-string)?
        #
        # Casos cazados aquí:
        #   {"type": ["click"]}
        #   {"intent": {"name": "search_youtube"}}
        #   {"action": 123}
        #
        # No los dejamos pasar silenciosamente — si la llave existe,
        # solo aceptamos ``str`` (o ausencia). Cualquier otra forma es
        # blob malformado y necesita reparación.
        invalid_fields: List[Tuple[str, str]] = []  # (key, type_name)
        for key in _EXECUTABLE_FIELDS:
            if key not in step:
                continue
            raw_val = step.get(key)
            if not _is_acceptable_executable_field_type(raw_val):
                invalid_fields.append((key, type(raw_val).__name__))

        if invalid_fields:
            descr = ", ".join(
                f'{k}=<{tn}>' for k, tn in invalid_fields
            )
            result.add(
                StrictValidationCode.INVALID_EXECUTABLE_FIELD,
                step_index=idx,
                step_repr=_short_step_repr(step),
                message=(
                    f"Step #{idx} tiene llaves ejecutables con tipo "
                    f"inválido ({descr}). type/intent/action solo "
                    "aceptan strings dentro del semantic plan."
                ),
            )
            continue

        # ¿alguno de type / intent / action cae en la blacklist?
        #
        # Cubrimos las tres llaves porque la contaminación legacy
        # puede llegar por cualquiera de ellas, ej:
        #   {"action": "click", "x": 100, "y": 200}
        #   {"intent": "mouse_scroll"}
        #   {"intent": "search_youtube", "type": "click", ...}
        #
        # La comparación está SIEMPRE normalizada (strip+lower), así
        # que ``"Click"``, ``"  click  "`` y ``"MOUSE_SCROLL"`` se
        # detectan igual y nadie cuela legacy aprovechando el casing.
        #
        # Aplicamos la regla "una marca legacy contamina todo el
        # step": aunque el step tenga un ``intent`` semántico válido,
        # si ALGUNA de las tres llaves trae un valor en la blacklist
        # bloqueamos. Mezclar intención semántica con tipo legacy es
        # exactamente la clase de contradicción que produce las
        # ejecuciones inestables que estamos cazando.
        legacy_hits: List[Tuple[str, str]] = []  # (key_name, raw_value)
        for key in _EXECUTABLE_FIELDS:
            raw_val = step.get(key)
            norm = _normalize_field_value(raw_val)
            if norm is not None and norm in _LEGACY_STEP_TYPES_BLACKLIST:
                legacy_hits.append((key, str(raw_val)))

        if legacy_hits:
            hit_descr = ", ".join(
                f'{k}="{v}"' for k, v in legacy_hits
            )
            result.add(
                StrictValidationCode.LEGACY_STEP,
                step_index=idx,
                step_repr=_short_step_repr(step),
                message=(
                    f"Step #{idx} contiene marcas legacy ({hit_descr}) "
                    "dentro del semantic plan. El semantic plan debe "
                    "contener solo intentions (open_app, "
                    "search_youtube, etc.), nunca acciones técnicas "
                    "crudas — ni siquiera mezcladas con un intent "
                    "semántico."
                ),
            )
            continue

        # ¿``type`` es semántico pero no canónico?
        #
        # Regla de producto: el plan aprobado debe estar limpio Y
        # canónico, no solo "entendible". Si alguien metió
        # ``{"type": "OPEN_APP"}`` o ``{"type": " open_app "}``, no
        # lo aprobamos silenciosamente — pedimos normalización
        # explícita (un normalizer previo o reparación manual). Así
        # el contrato del plan queda determinístico.
        raw_type = step.get("type")
        if isinstance(raw_type, str) and raw_type:
            norm_type = _normalize_field_value(raw_type)
            if (
                norm_type is not None
                and norm_type in SEMANTIC_TYPES
                and raw_type != norm_type
            ):
                result.add(
                    StrictValidationCode.NON_CANONICAL_TYPE,
                    step_index=idx,
                    step_repr=_short_step_repr(step),
                    message=(
                        f"Step #{idx} usa type=\"{raw_type}\" no "
                        f"canónico. Debe escribirse exactamente como "
                        f"\"{norm_type}\" (lowercase, sin espacios). "
                        "Normaliza antes de aprobar."
                    ),
                )
                continue

        # ¿forma semántica reconocible?
        if not _has_semantic_step_shape(step):
            result.add(
                StrictValidationCode.NON_SEMANTIC_STEP,
                step_index=idx,
                step_repr=_short_step_repr(step),
                message=(
                    f"Step #{idx} no tiene intent/action ni un type "
                    "reconocido como semántico."
                ),
            )
        else:
            try:
                from app.services.missions.uic_capability_registry import (
                    validate_step_against_registry,
                )
                uic_err = validate_step_against_registry(step)
            except Exception as exc:
                uic_err = f"UIC_REGISTRY_ERROR:{exc}"
            if uic_err:
                result.add(
                    StrictValidationCode.UNKNOWN_UIC_CAPABILITY,
                    step_index=idx,
                    step_repr=_short_step_repr(step),
                    message=(
                        f"Step #{idx} falló contrato UIC registry: {uic_err}"
                    ),
                )

    return result


# ─────────────────────────────────────────────────────────────────────
# READY status policy (PRD 2026-05-07c §C)
# ─────────────────────────────────────────────────────────────────────
#
# El plan puede quedar en ``READY`` SOLO cuando:
#   1. La validación estricta no encuentra contaminación legacy.
#   2. Los portadores semánticos (perfil/query/URL/app/…) están lo
#      suficientemente especificados — lo decide
#      :mod:`semantic_ambiguity_engine` (no reglas tipo-a-tipo aisladas).
#   3. ``coords_used == False``.
#   4. ``legacy_graph_ignored == True``.
#   5. ``compiled_execution_graph`` no participa en ejecución
#      (``mission.legacy_compiled_graph_purpose == "legacy_debug_only"``).
#   6. Sin ``needs_user_label`` espurio que indique texto humano incompleto
#      tras aplicar semantic ambiguity + EXECUTION_TRUTH.
#   7. No hay paths /temp/ dentro del semantic_execution_plan ni en
#      artifacts activos de la misión.
#   8. Rutas ejecutables UIC canónicas (Ctrl+t / Ctrl+l / filtros coords).


def _has_temp_paths_in_plan(plan_dict: Optional[Dict[str, Any]]) -> bool:
    """Inspecciona el blob serializado del plan en busca de strings con
    ``/assets/temp/``.
    """
    if not isinstance(plan_dict, dict):
        return False

    def _scan(obj: Any) -> bool:
        if isinstance(obj, str):
            s = obj.replace("\\", "/").lower()
            return "/assets/temp/" in s
        if isinstance(obj, dict):
            return any(_scan(v) for v in obj.values())
        if isinstance(obj, list):
            return any(_scan(v) for v in obj)
        return False

    return _scan(plan_dict.get("steps"))


def compute_ready_status(
    plan: SemanticExecutionPlan,
    *,
    mission: Any = None,
) -> Tuple[str, List[str]]:
    """Decide si ``plan`` puede pasar a ``READY``.

    Devuelve ``(status, blockers)`` donde:

      * ``status``: :data:`CollapseStatus.READY` cuando se cumplen
        TODOS los invariantes; :data:`CollapseStatus.NEEDS_REVIEW`
        cuando hay blockers reparables; :data:`CollapseStatus.COMPILED_DRAFT`
        cuando el plan está vacío o la validación estricta encuentra
        contaminación que NO es reparable.
      * ``blockers``: lista de códigos :class:`ReadyBlockerCode`
        explicando qué falta. Vacía cuando ``status == READY``.

    PRD 2026-05-08c (cierre LEGACY_RUNTIME_CONTAMINATION)
    -----------------------------------------------------

    El blocker ``LEGACY_RUNTIME_CONTAMINATION`` SOLO se emite cuando hay
    evidencia real de que algo legacy participa en ejecución o decisión
    (``affects_execution=True``). Los detalles de TODA la contaminación
    detectada (afecte o no la ejecución) quedan disponibles en
    ``mission._contamination_details`` para auditoría / UI experta.

    Casos que SÍ contaminan ejecución (afectan READY):
      * Validación estricta encuentra un step legacy DENTRO del plan
        (``LEGACY_STEP_INSIDE_SEMANTIC_PLAN``).
      * ``plan.coords_used == True`` (decisión primaria por coords).
      * ``plan.legacy_graph_ignored == False`` (el bridge mira legacy).
      * ``mission.legacy_compiled_graph_purpose != "legacy_debug_only"``
        con grafo legacy no vacío (sería doble ejecución).
      * Un ``preferred_strategy`` del plan apunta a una pista legacy
        prohibida (``vision_coords``, ``visual_asset``, ``use_coords``…).
      * Decisión registrada con ``decision.source == "compiled_execution_graph"``
        (alguien tomó decisión sobre el grafo legacy).
      * ``run_legacy_player_called == True`` registrado en la misión.
      * Ghost simulation marcada como simulando el grafo legacy.

    Casos que NO contaminan ejecución (van a contamination_details
    como debug pero NO bloquean READY):
      * ``compiled_execution_graph`` "sucio" pero marcado debug-only.
      * ``raw_trace`` con clicks/coords/scrolls (es evidencia, no plan).
      * ``target_signature.coordinates`` con bbox/xy (evidencia visual).
      * ``visual_assets`` con crops (artefactos de captura).
      * ``legacy_graph_ignored == True`` con grafo legacy presente.
    """
    blockers: List[str] = []
    contamination_details: List[Dict[str, Any]] = []

    def _add_contamination(
        kind: str,
        where: str,
        reason: str,
        *,
        source_module: str = "semantic_execution_plan",
        contaminating_value: Any = None,
        affects_execution: bool = True,
    ) -> None:
        """Registra el detalle de contaminación.

        Solo emite el blocker ``LEGACY_RUNTIME_CONTAMINATION`` cuando
        ``affects_execution=True``. Los casos meramente informativos
        (debug-only) quedan registrados en ``contamination_details``
        para la vista experto pero no bloquean READY.
        """
        contamination_details.append({
            "kind": kind,
            "where": where,
            "field_path": where,
            "source_module": source_module,
            "contaminating_value": contaminating_value,
            "reason": reason,
            "affects_execution": bool(affects_execution),
        })
        if affects_execution:
            blockers.append(ReadyBlockerCode.LEGACY_RUNTIME_CONTAMINATION)

    if not plan or not plan.steps:
        return (CollapseStatus.COMPILED_DRAFT,
                [ReadyBlockerCode.SEMANTIC_PLAN_NOT_READY])

    # 1) Validación estricta (delegada a la misión cuando está disponible).
    if mission is not None:
        try:
            strict = validate_semantic_execution_plan_strict(mission)
            if not strict.is_valid:
                # Cualquier violación estricta es contaminación legacy
                # dentro del plan — bloquea READY (afecta ejecución
                # porque el SmartExecutor consume estos steps).
                for v in (strict.violations or [])[:10]:
                    _add_contamination(
                        kind="strict_violation",
                        where=getattr(v, "path", "plan"),
                        reason=getattr(v, "message", str(v)),
                        source_module="semantic_execution_plan.strict_validator",
                        contaminating_value=getattr(v, "step_repr", None),
                        affects_execution=True,
                    )
                if not strict.violations:
                    _add_contamination(
                        kind="strict_violation",
                        where="plan",
                        reason="strict validation reported invalid",
                        source_module="semantic_execution_plan.strict_validator",
                        affects_execution=True,
                    )
        except Exception:
            pass

    # 2) coords_used / legacy_graph_ignored. Ambos son flags activos
    #    sobre la decisión del runtime — afectan ejecución directamente.
    if plan.coords_used:
        _add_contamination(
            kind="coords_used",
            where="plan.coords_used",
            reason=(
                "el plan marcó coords_used=True; la regla de oro es "
                "que NUNCA usemos coordenadas absolutas como camino "
                "primario."
            ),
            source_module="semantic_execution_plan",
            contaminating_value=True,
            affects_execution=True,
        )
    if not plan.legacy_graph_ignored:
        _add_contamination(
            kind="legacy_graph_active",
            where="plan.legacy_graph_ignored",
            reason=(
                "legacy_graph_ignored=False — el smart_runner aún "
                "tiene en cuenta el compiled_execution_graph como "
                "fuente de ejecución."
            ),
            source_module="semantic_execution_plan",
            contaminating_value=False,
            affects_execution=True,
        )

    # 3) compiled_execution_graph debug-only (si la misión está adjunta).
    #
    #    REGLA CENTRAL (PRD 2026-05-08c §A.2):
    #      compiled_execution_graph sucio = permitido como debug.
    #      compiled_execution_graph usado o considerado activo = contaminación.
    #
    #    Aceptamos cuatro purposes neutros:
    #      * "legacy_debug_only" — explícitamente debug.
    #      * None / vacío — no hay grafo legacy materializado.
    #    Cualquier otro valor con grafo no vacío SÍ contamina.
    if mission is not None:
        purpose = getattr(mission, "legacy_compiled_graph_purpose",
                          "execution")
        cgraph = getattr(mission, "compiled_execution_graph", None) or []
        if purpose == "legacy_debug_only" and cgraph:
            # Caso explícito permitido: registramos para auditoría
            # experta, pero NO bloqueamos READY.
            _add_contamination(
                kind="compiled_graph_debug_only",
                where="mission.compiled_execution_graph",
                reason=(
                    f"compiled_execution_graph (n={len(cgraph)}) está "
                    "marcado como 'legacy_debug_only'. Se conserva "
                    "como evidencia/debug, no participa en ejecución."
                ),
                source_module="approval_gate",
                contaminating_value=f"len={len(cgraph)}",
                affects_execution=False,
            )
        elif purpose not in ("legacy_debug_only", "", None) and cgraph:
            # Grafo legacy con purpose ejecutable y no vacío:
            # potencial doble ejecución → bloquea.
            _add_contamination(
                kind="compiled_graph_executable",
                where="mission.legacy_compiled_graph_purpose",
                reason=(
                    f"compiled_execution_graph (n={len(cgraph)}) "
                    f"sigue marcado como '{purpose}' en lugar "
                    f"de 'legacy_debug_only'."
                ),
                source_module="approval_gate",
                contaminating_value=purpose,
                affects_execution=True,
            )

    # 3b) Banderas de runtime registradas por el bridge / smart_executor.
    #     Estos atributos son escritos por el SmartRunnerBridge cuando
    #     una decisión real cae en legacy — son la prueba más directa
    #     de contaminación EN EJECUCIÓN.
    if mission is not None:
        if getattr(mission, "_run_legacy_player_called", False):
            _add_contamination(
                kind="run_legacy_player_called",
                where="mission._run_legacy_player_called",
                reason=(
                    "run_legacy_player fue invocado durante una "
                    "ejecución; existiendo semantic_execution_plan, "
                    "esto es contaminación de runtime."
                ),
                source_module="smart_runner_bridge",
                contaminating_value=True,
                affects_execution=True,
            )
        decision = getattr(mission, "_last_decision", None)
        if isinstance(decision, dict):
            d_source = (decision.get("source") or "").strip().lower()
            if d_source in ("compiled_execution_graph", "legacy_graph"):
                _add_contamination(
                    kind="decision_source_legacy",
                    where="mission._last_decision.source",
                    reason=(
                        f"decision.source={d_source!r} — el bridge "
                        "tomó una decisión basada en el grafo legacy "
                        "en lugar del plan semántico."
                    ),
                    source_module="smart_runner_bridge",
                    contaminating_value=d_source,
                    affects_execution=True,
                )
        ghost_meta = getattr(mission, "_ghost_simulation_meta", None)
        if isinstance(ghost_meta, dict):
            g_source = (
                ghost_meta.get("simulating") or ""
            ).strip().lower()
            if g_source in (
                "compiled_execution_graph", "legacy_graph",
            ):
                _add_contamination(
                    kind="ghost_simulating_legacy",
                    where="mission._ghost_simulation_meta.simulating",
                    reason=(
                        "Ghost Simulation está simulando el grafo "
                        "legacy en lugar del semantic_execution_plan."
                    ),
                    source_module="ghost_simulation",
                    contaminating_value=g_source,
                    affects_execution=True,
                )

    # 4) Semantic Ambiguity Resolution (universal carriers) + saneamiento
    #    UIC de estrategias canónicas. Los bloqueos humanos sólo llegan desde
    #    semantic_ambiguity_engine sobre texto de misión (+ señales de
    #    captura como query_fidelity); no existe disparador tipo
    #    ``if step.type == …`` para contenido textual.
    try:
        from app.services.missions.semantic_ambiguity_engine import (
            aggregate_plan_ambiguity,
            attach_semantic_ambiguity_audit_to_mission,
            apply_ambiguity_based_needs_label_relief,
            evaluate_plan_semantic_ambiguity,
        )
        sar_steps = evaluate_plan_semantic_ambiguity(plan, mission)
        apply_ambiguity_based_needs_label_relief(plan, mission, sar_steps)
        try:
            from app.services.runtime.entity_resolution_layer import (
                apply_entity_resolution_relief,
            )
            apply_entity_resolution_relief(plan, mission)
        except Exception as e:
            log.debug(f"[ready_status] entity_resolution_relief: {e}")
        try:
            from app.services.runtime.universal_collection_entity_engine import (
                apply_collection_entity_ready_relief,
                apply_pcof_ready_relief,
            )
            apply_pcof_ready_relief(plan, mission)
            apply_collection_entity_ready_relief(plan, mission)
        except Exception as e:
            log.debug(f"[ready_status] collection_entity_ready_relief: {e}")
        try:
            from app.services.runtime.universal_collection_entity_engine import (
                compute_uces_ready_blockers,
            )
            for sp in plan.steps:
                for rb in compute_uces_ready_blockers(sp):
                    if rb:
                        blockers.append(rb)
        except Exception as e:
            log.debug(f"[ready_status] uces_ready_blockers: {e}")
        sar_agg = aggregate_plan_ambiguity(sar_steps)
        if mission is not None:
            attach_semantic_ambiguity_audit_to_mission(
                mission,
                step_results=sar_steps,
                plan_aggregate=sar_agg,
            )
        for sar in sar_steps:
            for rb in sar.readiness_blockers:
                if rb:
                    blockers.append(rb)
    except Exception as e:
        log.debug(f"[ready_status] semantic_ambiguity_bundle: {e}")

    # 5) Preferencias ejecutables UIC (coords prohibidos como primarias,
    #    rutas Ctrl+L/Ctrl+t canónicas, etc.).
    for idx, sp in enumerate(plan.steps):
        path = f"plan.steps[{idx}:{sp.type}]"
        primary_low = (sp.preferred_strategy or "").lower()
        if any(h in primary_low for h in _FORBIDDEN_PRIMARY_HINTS):
            _add_contamination(
                kind="primary_strategy_forbidden",
                where=f"{path}.preferred_strategy",
                reason=(
                    f"preferred_strategy={sp.preferred_strategy!r} "
                    "contiene una pista legacy/coords prohibida."
                ),
                source_module="semantic_execution_plan",
                contaminating_value=sp.preferred_strategy,
                affects_execution=True,
            )
        if sp.type in ("open_url", "open_site"):
            canonical_primaries = (
                "open_url:hotkey_ctrl_l", "open_site:smart_route",
            )
            if sp.preferred_strategy not in canonical_primaries:
                _add_contamination(
                    kind="primary_strategy_off_canon",
                    where=f"{path}.preferred_strategy",
                    reason=(
                        f"preferred_strategy={sp.preferred_strategy!r} "
                        "no es canónica para apertura de URL "
                        "(open_url:hotkey_ctrl_l u open_site:smart_route)."
                    ),
                    source_module="semantic_execution_plan",
                    contaminating_value=sp.preferred_strategy,
                    affects_execution=True,
                )
        elif sp.type == "open_new_tab":
            if sp.preferred_strategy != "open_new_tab:hotkey_ctrl_t":
                _add_contamination(
                    kind="primary_strategy_off_canon",
                    where=f"{path}.preferred_strategy",
                    reason=(
                        f"preferred_strategy={sp.preferred_strategy!r} "
                        "no es la canónica 'open_new_tab:hotkey_ctrl_t'."
                    ),
                    source_module="semantic_execution_plan",
                    contaminating_value=sp.preferred_strategy,
                    affects_execution=True,
                )

    # 6) Asset finalizer: SIEMPRE corre antes de evaluar /temp/. Es
    #    idempotente y no toca nada si ya está canonical (PRD §F).
    if mission is not None:
        try:
            from app.services.missions.asset_finalizer import (
                finalize_mission_assets, has_temp_paths,
            )
            try:
                finalize_mission_assets(mission)
            except Exception as e:
                log.debug(f"[ready_status] finalize_assets falló: {e}")

            plan_blob = getattr(mission, "semantic_execution_plan", None)
            if _has_temp_paths_in_plan(plan_blob):
                blockers.append(ReadyBlockerCode.TEMP_ASSET_IN_APPROVED_PLAN)

            if has_temp_paths(mission):
                purpose = getattr(mission, "legacy_compiled_graph_purpose",
                                  "execution")
                if purpose != "legacy_debug_only":
                    blockers.append(
                        ReadyBlockerCode.TEMP_ASSET_IN_APPROVED_PLAN
                    )
        except Exception as e:
            log.debug(f"[ready_status] asset finalizer no disponible: {e}")

    # Anotación auxiliar: detalles de contaminación al alcance de la UI.
    if mission is not None and contamination_details:
        try:
            mission._contamination_details = contamination_details
        except Exception:
            pass

    # 7) PRD 2026-05-10 §A: dropped semantic events.
    #    El engine guarda en ``mission._dropped_semantic_events`` la
    #    lista de eventos relevantes del raw_trace que NO quedaron
    #    cubiertos por ningún step semántico ni por noise audit.
    #    Mientras la lista no sea vacía, el plan NO puede pasar a
    #    READY: hubo intención humana real que el engine no logró
    #    representar y un humano debe revisar.
    if mission is not None:
        dropped = getattr(mission, "_dropped_semantic_events", None) or []
        if dropped:
            blockers.append(
                ReadyBlockerCode.SEMANTIC_EVENT_DROPPED_FROM_PLAN
            )

    # Dedup preservando orden.
    seen = set()
    uniq: List[str] = []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            uniq.append(b)

    if uniq:
        return (CollapseStatus.NEEDS_REVIEW, uniq)
    return (CollapseStatus.READY, [])


READY_PROVENANCE_RAW = "raw_evidence"
READY_PROVENANCE_USER = "user_confirmation"
READY_PROVENANCE_ALIAS = "alias_store"
READY_PROVENANCE_UNKNOWN = "unknown"


from app.services.missions.execution_truth_engine import (
    attach_execution_truth_audit,
    is_execution_truth_confirmed,
)


def mission_has_trusted_pre_action_evidence(mission: Any) -> bool:
    """API retrocompatible → :func:`is_execution_truth_confirmed`."""
    return is_execution_truth_confirmed(mission)


def compute_ready_provenance(
    plan: SemanticExecutionPlan,
    *,
    mission: Any = None,
) -> str:
    """¿De dónde viene el READY del plan?

    Reglas:

      * Si el plan no está READY → ``"unknown"``.
      * Si la misión tiene al menos una entrada en
        ``review_confirmations`` con ``source == "user_confirmation"``
        o ``source == "heuristic_suggestion_accepted"`` → tu plan
        llegó a READY porque un humano confirmó algo. Procedencia =
        ``"user_confirmation"``.
      * Si solo hay confirmaciones de tipo ``"alias_store"`` (datos
        recuperados automáticamente de runs anteriores) →
        ``"alias_store"``.
      * Si no hay ninguna confirmación → ``"raw_evidence"`` (la
        captura fue limpia, no hizo falta humano).

    Esta clasificación es la base del panel "Confirmado por usuario"
    que la UI muestra en modo experto. Internamente, READY siempre
    es READY — pero la **procedencia** queda explícita para
    auditoría.
    """
    if not plan or plan.intent_status not in (
        CollapseStatus.READY,
        CollapseStatus.EXECUTABLE,
    ):
        return READY_PROVENANCE_UNKNOWN
    if mission is None:
        return READY_PROVENANCE_RAW
    confs = list(getattr(mission, "review_confirmations", None) or [])
    if not confs:
        return READY_PROVENANCE_RAW
    sources = {getattr(c, "source", "") for c in confs}
    user_sources = {"user_confirmation", "heuristic_suggestion_accepted"}
    if sources & user_sources:
        return READY_PROVENANCE_USER
    if "alias_store" in sources:
        return READY_PROVENANCE_ALIAS
    return READY_PROVENANCE_USER  # default conservador


def attach_intent_status_to_plan(
    plan: SemanticExecutionPlan,
    *,
    mission: Any = None,
) -> SemanticExecutionPlan:
    """Recalcula y persiste ``intent_status`` + ``not_ready_reasons``
    + ``ready_provenance``.

    Mutación in-place. Útil para que ``smart_runner_bridge`` /
    ``approval_gate`` no tengan que re-correr la lógica cada vez.
    """
    if mission is not None:
        try:
            attach_execution_truth_audit(mission)
        except Exception as e:
            log.debug(f"[attach_intent_status] execution_truth_audit: {e}")
    status, blockers = compute_ready_status(plan, mission=mission)
    plan.intent_status = status
    plan.not_ready_reasons = list(blockers)
    plan.ready_provenance = compute_ready_provenance(plan, mission=mission)
    return plan


__all__ = [
    "SemanticExecutionPlan",
    "SemanticPlanStep",
    "SemanticPlanValidation",
    "SemanticPlanViolation",
    "SEMANTIC_TYPES",
    "StrictValidationCode",
    "ReadyBlockerCode",
    "READY_BLOCKER_CODES",
    "build_semantic_execution_plan",
    "attach_semantic_plan_to_mission",
    "semantic_plan_to_mission_steps",
    "has_semantic_plan",
    "has_valid_semantic_execution_plan",
    "validate_semantic_execution_plan_strict",
    "compute_ready_status",
    "compute_ready_provenance",
    "attach_intent_status_to_plan",
    "mission_has_trusted_pre_action_evidence",
    "is_generic_profile_label",
    "READY_PROVENANCE_RAW",
    "READY_PROVENANCE_USER",
    "READY_PROVENANCE_ALIAS",
    "READY_PROVENANCE_UNKNOWN",
]
