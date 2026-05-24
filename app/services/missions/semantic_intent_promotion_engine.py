"""
Nevlan — Semantic Intent Promotion Engine (SIPE)
================================================

Filosofía
---------

    No guardar "qué hizo físicamente el usuario".
    Guardar "qué quería lograr".

El recorder de Nevlan ya captura señales sobradas: teclado, mouse, UIA,
DOM, OCR, fingerprints visuales, ``post_action_state``, ``outcome``,
``action_groups``, ``field_session``. El problema NO es ver — es
**entender**.

Esta capa promueve esas señales crudas a **intenciones humanas
ejecutables genéricas**. Convierte:

* click + escribir + Enter en un searchbox de YouTube → ``search_site
  (site=youtube, query=...)``
* click en bookmark / address bar con ``youtube.com`` → ``open_site
  (site=youtube, url=...)``
* click en barra del navegador + texto sin TLD + Enter → ``web_search
  (query=...)``
* click en Windows Search + nombre app + click resultado → ``open_app
  (name=..., method="windows_search")``
* N scrolls consecutivos en el mismo contexto → ``scroll_results
  (amount=N)``

Patrones genéricos
------------------

El usuario explícitamente exigió **patrones genéricos reutilizables**:

* ``open_app(name, method)``           — cualquier app de Windows.
* ``select_profile(profile_name, app)`` — cualquier navegador.
* ``open_new_tab()``                    — Ctrl+T.
* ``open_site(site, url)``              — cualquier sitio web.
* ``search_site(site, query)``          — búsqueda dentro de un sitio.
* ``web_search(query, engine)``         — búsqueda en buscador.
* ``scroll_results(direction, amount)`` — scroll agregado.

Nada hardcodea YouTube. ``site="youtube"`` es solo un valor del
parámetro genérico — el ``ActionRunner`` decide la estrategia
específica al ejecutar (ver ``search_site:smart_route``).

Reglas duras
------------

1. ``coords_used = False`` salvo emergencia explícita.
2. ``legacy_graph_ignored = True`` cuando hay SEP construible.
3. Si NO se puede construir SEP → ``status = NEEDS_REVIEW`` con
   ``not_ready_reasons`` claros. **Nunca** ejecutar legacy
   automáticamente.
4. ``profile_name`` vacío + picker visible → ``needs_user_label=True``,
   no regrabación.
5. ``query`` con ``query_fidelity_status != ok`` → ``needs_user_label
   =True``, no inventar.
6. ``outcome`` sirve para **validar** intención, NO para inventar
   target.
7. Clicks que solo prepararon un ``field_session`` se absorben en la
   intención superior.
8. Scrolls consecutivos en el mismo contexto se agregan en uno solo
   con ``amount = N``.

API
~~~

>>> from app.services.missions.semantic_intent_promotion_engine import (
...     promote_semantic_intent,
...     apply_promotion_to_mission,
... )
>>> result = promote_semantic_intent(mission)
>>> result.plan.steps                    # tipos genéricos
[SemanticPlanStep(type='open_app', ...),
 SemanticPlanStep(type='select_profile', ...),
 SemanticPlanStep(type='open_new_tab', ...),
 SemanticPlanStep(type='open_site', ...),
 SemanticPlanStep(type='search_site', ...),
 SemanticPlanStep(type='scroll_results', ...)]
>>> result.plan.source
'semantic_intent_promotion_engine'
>>> result.plan.coords_used
False
>>> result.plan.legacy_graph_ignored
True

>>> apply_promotion_to_mission(mission)  # mutación in-place
>>> mission.semantic_execution_plan["source"]
'semantic_intent_promotion_engine'
>>> mission.legacy_compiled_graph_purpose
'legacy_debug_only'
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import Mission
from app.core.logger import log
from app.services.missions.intent_collapse_engine import (
    CollapsedStep,
    CollapseStatus,
    MissionIntentPlan,
    apply_collapse_to_mission,
    collapse_intent,
)
from app.services.missions.semantic_execution_plan import (
    SemanticExecutionPlan,
    SemanticPlanStep,
    _STRATEGY_BY_TYPE,
    _strip_forbidden,
    attach_intent_status_to_plan,
    is_generic_profile_label,
)


# ─────────────────────────────────────────────────────────────────────
# Catálogo de sitios reconocidos (genérico, extensible).
# ─────────────────────────────────────────────────────────────────────
#
# El engine NO hardcodea YouTube. ``site`` es un alias canónico para
# UX/logs y para que el ``smart_route`` del executor pueda elegir la
# estrategia específica cuando una existe (ej. ``search_youtube
# :dom_input``). Cualquier sitio nuevo se cubre añadiendo un par
# ``(domain → alias)`` aquí — sin tocar el resto del pipeline.
_SITE_DOMAIN_TO_ALIAS: Dict[str, str] = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "www.youtube.com": "youtube",
    "google.com": "google",
    "www.google.com": "google",
    "google.es": "google",
    "google.com.mx": "google",
    "bing.com": "bing",
    "www.bing.com": "bing",
    "duckduckgo.com": "duckduckgo",
    "github.com": "github",
    "www.github.com": "github",
    "gmail.com": "gmail",
    "mail.google.com": "gmail",
    "drive.google.com": "drive",
    "twitter.com": "twitter",
    "x.com": "x",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "linkedin.com": "linkedin",
    "reddit.com": "reddit",
}

# URLs por defecto cuando solo conocemos el alias (e.g. usuario clicó
# bookmark "YouTube" sin URL extraída).
_SITE_ALIAS_TO_URL: Dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "bing": "https://www.bing.com",
    "duckduckgo": "https://duckduckgo.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "drive": "https://drive.google.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com",
    "linkedin": "https://www.linkedin.com",
    "reddit": "https://www.reddit.com",
}

# Buscadores reconocidos cuando una búsqueda llega por la omnibox del
# navegador. ``search_web`` ya cubre "Google" como default; aquí solo
# refinamos el ``engine`` cuando la URL post-acción lo indica.
_SEARCH_ENGINE_HOSTS: Dict[str, str] = {
    "google.com": "google",
    "bing.com": "bing",
    "duckduckgo.com": "duckduckgo",
    "search.brave.com": "brave",
}


# Códigos de blocker propios del engine (complementan los del SEP).
class PromotionBlockerCode:
    """Códigos cortos que aparecen en
    :class:`PromotionResult.blockers` cuando el engine no consigue
    promover una intención completa.
    """

    EMPTY_RAW_TRACE = "EMPTY_RAW_TRACE"
    NO_SEMANTIC_SIGNAL = "NO_SEMANTIC_SIGNAL"
    AMBIGUOUS_PROFILE_PICKER = "AMBIGUOUS_PROFILE_PICKER"
    QUERY_FIDELITY_SUSPECT = "QUERY_FIDELITY_SUSPECT"
    OUTCOME_DOES_NOT_MATCH_INTENT = "OUTCOME_DOES_NOT_MATCH_INTENT"


# ─────────────────────────────────────────────────────────────────────
# Resultado del engine
# ─────────────────────────────────────────────────────────────────────


@dataclass
class PromotionResult:
    """Resultado público del Semantic Intent Promotion Engine.

    Atributos:
        plan: el ``SemanticExecutionPlan`` ya promovido a tipos
            genéricos. Si el engine no logró producir un plan
            ejecutable, ``plan.steps`` puede estar vacío y
            ``plan.intent_status`` quedará en ``NEEDS_REVIEW`` o
            ``COMPILED_DRAFT``.
        intent_plan: el ``MissionIntentPlan`` interno usado como
            fuente de verdad (output del intent_collapse_engine).
            Útil para debug / auditoría.
        promotions: traza de cada promoción aplicada (legacy_type →
            generic_type) con el motivo. Útil para Mission Review en
            modo experto y para tests.
        blockers: códigos de :class:`PromotionBlockerCode` que
            explican por qué el plan NO está READY (en adición a los
            ``not_ready_reasons`` que computa el SEP).
    """

    plan: SemanticExecutionPlan
    intent_plan: MissionIntentPlan
    promotions: List[Dict[str, Any]] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    @property
    def is_executable(self) -> bool:
        return (
            bool(self.plan.steps)
            and self.plan.intent_status == CollapseStatus.READY
            and not self.blockers
        )


# ─────────────────────────────────────────────────────────────────────
# Helpers de promoción (legacy → generic).
# ─────────────────────────────────────────────────────────────────────


def _resolve_site_from_url(url: str, name_hint: str = "") -> str:
    """Mapea una URL a un alias canónico de sitio.

    Acepta URLs sin protocolo (``"youtube.com/..."``). Devuelve string
    vacío si no hay coincidencia — el caller debe tratar eso como
    "sitio desconocido pero válido".
    """
    blob = f"{url} {name_hint}".lower()
    if not blob.strip():
        return ""
    for domain, alias in _SITE_DOMAIN_TO_ALIAS.items():
        if domain in blob:
            return alias
    # Heurística de fallback: el primer token "x.y" del blob.
    for tok in blob.replace("/", " ").replace(":", " ").split():
        if "." in tok and not tok.startswith("."):
            return tok.split(".")[0].strip().lower()
    return ""


def _canonical_url_for(site: str, url: str) -> str:
    """Devuelve la URL canónica completa para ``site``.

    Si ``url`` ya viene completa (con protocolo), se respeta. Si no,
    se busca el default en :data:`_SITE_ALIAS_TO_URL`.
    """
    u = (url or "").strip()
    if u.startswith(("http://", "https://")):
        return u
    if u and "://" not in u:
        # Tiene dominio sin protocolo (e.g. "youtube.com").
        return f"https://{u.lstrip('/')}"
    canonical = _SITE_ALIAS_TO_URL.get((site or "").lower().strip())
    return canonical or u


def _human_label_open_app(name: str, method: str) -> str:
    nice = name.capitalize() if name else "app"
    if method == "windows_search":
        return f"Abrir {nice}"
    return f"Abrir {nice}"


def _human_label_select_profile(profile: str) -> str:
    if profile:
        return f"Seleccionar perfil {profile}"
    return "Seleccionar perfil"


def _human_label_open_new_tab() -> str:
    return "Abrir nueva pestaña"


def _human_label_open_site(site: str, url: str) -> str:
    if site:
        return f"Abrir {site.capitalize()}"
    if url:
        return f"Abrir {url}"
    return "Abrir sitio"


def _human_label_search_site(site: str, query: str) -> str:
    if site and query:
        return f"Buscar \u201c{query}\u201d en {site.capitalize()}"
    if query:
        return f"Buscar \u201c{query}\u201d"
    return "Buscar"


def _human_label_web_search(engine: str, query: str) -> str:
    if engine and query:
        return f"Buscar \u201c{query}\u201d en {engine.capitalize()}"
    if query:
        return f"Buscar \u201c{query}\u201d en la web"
    return "Buscar en la web"


def _human_label_scroll_results(direction: str, amount: int) -> str:
    arrow = "abajo" if direction == "down" else "arriba"
    if amount and amount > 1:
        return f"Bajar resultados {amount} veces" if direction == "down" \
            else f"Subir resultados {amount} veces"
    return f"Bajar un poco" if direction == "down" else f"Subir un poco"


# ─────────────────────────────────────────────────────────────────────
# Promoción de un step concreto.
# ─────────────────────────────────────────────────────────────────────


def _promote_one_step(
    sp: SemanticPlanStep,
    *,
    promotions: List[Dict[str, Any]],
) -> SemanticPlanStep:
    """Devuelve el step promovido (puede ser el mismo) con tipo
    genérico cuando aplica.

    NUNCA modifica ``sp`` in-place — devuelve una copia con los
    cambios. Si el tipo ya es genérico, se devuelve casi tal cual
    (solo se refresca el ``human_label`` y la estrategia canónica
    si está vacía).
    """
    t = sp.type
    new_type = t
    new_params = dict(sp.params)
    label = sp.human_label

    # ── search_youtube → search_site(site=youtube, query=...) ──────
    if t == "search_youtube":
        query = (sp.params.get("query") or "").strip()
        new_type = "search_site"
        new_params = {
            "site": "youtube",
            "query": query,
            "submit": True,
        }
        # Conservamos query_fidelity_status para que el SEP siga
        # validando fidelidad (compute_ready_status lee este campo).
        if sp.params.get("query_fidelity_status"):
            new_params["query_fidelity_status"] = sp.params["query_fidelity_status"]
        if sp.params.get("suggested_query"):
            new_params["suggested_query"] = sp.params["suggested_query"]
        label = _human_label_search_site("youtube", query)
        promotions.append({
            "from": "search_youtube",
            "to": "search_site",
            "site": "youtube",
            "reason": "promotion to generic search_site pattern",
        })

    # ── search_content(provider/site=youtube) → search_site ───────
    elif t == "search_content":
        prov = str(sp.params.get("provider") or "").strip().lower()
        site_hint = str(sp.params.get("site") or "").strip().lower()
        if prov == "youtube" or site_hint == "youtube":
            query = (sp.params.get("query") or "").strip()
            new_type = "search_site"
            new_params = {
                "site": "youtube",
                "query": query,
                "submit": True,
            }
            if sp.params.get("query_fidelity_status"):
                new_params["query_fidelity_status"] = (
                    sp.params["query_fidelity_status"]
                )
            if sp.params.get("suggested_query"):
                new_params["suggested_query"] = sp.params["suggested_query"]
            if sp.params.get("suspected_missing_connector"):
                new_params["suspected_missing_connector"] = (
                    sp.params["suspected_missing_connector"]
                )
            label = _human_label_search_site("youtube", query)
            promotions.append({
                "from": "search_content",
                "to": "search_site",
                "site": "youtube",
                "reason": (
                    "promotion to generic search_site pattern "
                    "(from canonical search_content)"
                ),
            })

    # ── search_web → web_search(query=..., engine=...) ─────────────
    elif t == "search_web":
        query = (sp.params.get("query") or "").strip()
        engine = (sp.params.get("engine") or "google").strip().lower() or "google"
        new_type = "web_search"
        new_params = {
            "query": query,
            "engine": engine,
            "submit": bool(sp.params.get("submit", True)),
        }
        label = _human_label_web_search(engine, query)
        promotions.append({
            "from": "search_web",
            "to": "web_search",
            "engine": engine,
            "reason": "promotion to generic web_search pattern",
        })

    # ── open_url → open_site(site=alias, url=...) ──────────────────
    elif t == "open_url":
        url = (sp.params.get("url") or "").strip()
        alias = (sp.params.get("alias") or "").strip().lower()
        site = alias or _resolve_site_from_url(url)
        canonical_url = _canonical_url_for(site, url)
        new_type = "open_site"
        new_params = {}
        if site:
            new_params["site"] = site
        if canonical_url:
            new_params["url"] = canonical_url
        elif url:
            new_params["url"] = url
        label = _human_label_open_site(site, canonical_url or url)
        promotions.append({
            "from": "open_url",
            "to": "open_site",
            "site": site,
            "url": canonical_url or url,
            "reason": "promotion to generic open_site pattern",
        })

    # ── open_app: solo refrescamos el human_label canónico ─────────
    elif t == "open_app":
        name = (sp.params.get("name") or "").strip()
        method = (sp.params.get("method") or "windows_search").strip()
        label = _human_label_open_app(name, method)

    elif t == "select_profile":
        profile = (sp.params.get("profile_name")
                   or sp.params.get("profile") or "").strip()
        label = _human_label_select_profile(profile)

    elif t == "open_new_tab":
        label = _human_label_open_new_tab()

    elif t == "scroll_results":
        direction = (sp.params.get("direction") or "down").strip().lower()
        try:
            amount = max(1, int(sp.params.get("amount") or 1))
        except Exception:
            amount = 1
        label = _human_label_scroll_results(direction, amount)

    # ── Fix preferred_strategy si quedó out-of-sync con el tipo ────
    plan = _STRATEGY_BY_TYPE.get(new_type)
    if plan is not None:
        # Solo sobreescribimos cuando hubo promoción de tipo o cuando
        # la primaria actual ya no es válida para el tipo nuevo.
        if new_type != t or not sp.preferred_strategy or not sp.preferred_strategy.startswith(new_type + ":"):
            preferred = plan.preferred
            fallbacks, _ = _strip_forbidden(plan.fallbacks)
        else:
            preferred = sp.preferred_strategy
            fallbacks = list(sp.fallback_strategies)
    else:
        preferred = sp.preferred_strategy
        fallbacks = list(sp.fallback_strategies)

    return SemanticPlanStep(
        id=sp.id,
        type=new_type,
        params=new_params,
        preferred_strategy=preferred,
        fallback_strategies=list(fallbacks),
        confidence=float(sp.confidence),
        human_label=label or sp.human_label or new_type,
        needs_user_label=bool(sp.needs_user_label),
        label_prompt=sp.label_prompt,
        block_id=sp.block_id,
    )


# ─────────────────────────────────────────────────────────────────────
# Outcome-aware promotion: usar outcome para VALIDAR, no para inventar.
# ─────────────────────────────────────────────────────────────────────

_BROWSER_PROCS = ("chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "firefox.exe")


def _outcome_validates_open_app(mission: Mission, app_name: str) -> bool:
    """¿El raw_trace muestra que ``app_name`` quedó activa después?

    Buscamos un ``WINDOW_ACTIVATE`` con ``process_name`` matching tras
    el último click de Windows Search. Esto VALIDA que la promoción
    a ``open_app`` fue correcta — no inventa nada.
    """
    target = (app_name or "").strip().lower()
    if not target:
        return False
    expected_proc = f"{target}.exe"
    for ev in (mission.raw_trace or []):
        ctx = getattr(ev, "window_context", None)
        proc = (getattr(ctx, "process_name", "") or "").lower()
        if proc == expected_proc or proc.startswith(target):
            return True
    return False


def _outcome_validates_open_site(mission: Mission, site: str) -> bool:
    """¿Hay evidencia de navegación al sitio en el raw_trace?

    Inspecciona títulos y ``web_data.url`` para ver si el dominio del
    sitio aparece en algún evento posterior.
    """
    site_norm = (site or "").strip().lower()
    if not site_norm:
        return False
    for ev in (mission.raw_trace or []):
        ctx = getattr(ev, "window_context", None)
        title = (getattr(ctx, "title", "") or "").lower()
        if site_norm in title:
            return True
        meta = getattr(ev, "metadata", None) or {}
        web = meta.get("web_data") or {} if isinstance(meta, dict) else {}
        url = (web.get("url") or "").lower()
        if site_norm in url:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Entradas públicas
# ─────────────────────────────────────────────────────────────────────


def _deserialize_existing_sep(mission: Mission) -> List[SemanticPlanStep]:
    """Deserializa los steps del SEP existente a :class:`SemanticPlanStep`.

    Caso de uso: una misión cargada del store que ya tiene un SEP con
    tipos legacy (``search_youtube``, ``open_url``, ``search_web``)
    pero el ``raw_trace`` está vacío. Conservamos la info que el SEP
    ya tiene; la promoción a tipos genéricos la aplica el caller en
    el paso uniforme de promoción (más adelante en el pipeline).
    """
    sep = getattr(mission, "semantic_execution_plan", None)
    if not isinstance(sep, dict):
        return []
    out: List[SemanticPlanStep] = []
    for raw in (sep.get("steps") or []):
        if not isinstance(raw, dict):
            continue
        try:
            out.append(SemanticPlanStep.from_dict(raw))
        except Exception:
            continue
    return out


def promote_semantic_intent(
    mission: Mission,
    *,
    intent_plan: Optional[MissionIntentPlan] = None,
) -> PromotionResult:
    """Construye un :class:`PromotionResult` a partir de ``mission``.

    El engine es **puro** — no toca disco, no llama al LLM. Para
    persistir el plan en la misión, usar :func:`apply_promotion_to_mission`.

    Args:
        mission: la misión a analizar.
        intent_plan: opcional. Si se pasa, se usa como fuente y se
            evita re-correr el ``intent_collapse_engine`` (útil para
            tests deterministas).

    Returns:
        ``PromotionResult`` con el plan promovido, las trazas de
        promoción y los blockers detectados.
    """
    promotions: List[Dict[str, Any]] = []
    blockers: List[str] = []

    has_raw_trace = bool(mission.raw_trace)
    has_existing_sep_steps = bool(
        isinstance(mission.semantic_execution_plan, dict)
        and (mission.semantic_execution_plan.get("steps") or [])
    )

    if (
        not (mission.raw_trace or mission.compiled_execution_graph
             or mission.interpreted_steps)
        and not has_existing_sep_steps
    ):
        blockers.append(PromotionBlockerCode.EMPTY_RAW_TRACE)

    from app.services.missions.semantic_execution_plan import (
        _convert_collapsed_step,
        _dedup_and_order,
    )

    raw_steps: List[SemanticPlanStep] = []
    used_existing_sep = False

    if intent_plan is not None:
        # El caller pasó un intent_plan explícito — siempre lo
        # respetamos sobre el SEP persistido (caso test).
        for i, cs in enumerate(intent_plan.semantic_steps or []):
            sp = _convert_collapsed_step(cs, index=i, mission_id=(
                mission.id or "m"
            ))
            if sp is not None:
                raw_steps.append(sp)
        if not intent_plan.semantic_steps:
            blockers.append(PromotionBlockerCode.NO_SEMANTIC_SIGNAL)
    else:
        # Sin intent_plan explícito, decidimos según las señales.
        #
        # Camino A — el ICE no tiene base útil para reconstruir el
        # plan (no hay raw_trace) PERO la misión ya tiene un SEP
        # persistido con steps. Reusamos esos steps; la promoción
        # uniforme del paso 3 los procesará igual que cualquier otro.
        # Esto evita que SIPE destruya un SEP existente al
        # re-correr el ICE sobre un trace vacío (caso típico de
        # misiones cargadas del store que no conservaron el
        # raw_trace, o de tests con SEP pre-construido).
        if not has_raw_trace and has_existing_sep_steps:
            raw_steps = _deserialize_existing_sep(mission)
            existing_status = (
                mission.semantic_execution_plan.get("intent_status")
                or CollapseStatus.COMPILED_DRAFT
            )
            intent_plan = MissionIntentPlan(status=existing_status)
            used_existing_sep = True
        else:
            # Camino B — flujo normal: corremos el ICE.
            try:
                intent_plan = collapse_intent(mission=mission)
            except Exception as e:
                log.exception(f"[sipe] collapse_intent falló: {e}")
                intent_plan = MissionIntentPlan()

            if not intent_plan.semantic_steps:
                blockers.append(PromotionBlockerCode.NO_SEMANTIC_SIGNAL)

            # Si el ICE no produjo nada pero la misión ya tenía
            # un SEP utilizable, recuperamos esos steps en lugar
            # de borrar el plan (defensivo: el ICE puede degradarse
            # en grabaciones edge case y el SEP persistido es la
            # mejor verdad disponible).
            if not intent_plan.semantic_steps and has_existing_sep_steps:
                raw_steps = _deserialize_existing_sep(mission)
                existing_status = (
                    mission.semantic_execution_plan.get("intent_status")
                    or intent_plan.status
                    or CollapseStatus.COMPILED_DRAFT
                )
                intent_plan = MissionIntentPlan(status=existing_status)
                used_existing_sep = True
                # NO_SEMANTIC_SIGNAL ya no aplica: hay señal en el SEP.
                if PromotionBlockerCode.NO_SEMANTIC_SIGNAL in blockers:
                    blockers.remove(PromotionBlockerCode.NO_SEMANTIC_SIGNAL)
            else:
                mid = (mission.id or "m").strip() or "m"
                for i, cs in enumerate(intent_plan.semantic_steps or []):
                    sp = _convert_collapsed_step(cs, index=i, mission_id=mid)
                    if sp is not None:
                        raw_steps.append(sp)

    # 3) Promoción a tipos genéricos.
    promoted: List[SemanticPlanStep] = []
    for sp in raw_steps:
        promoted_step = _promote_one_step(sp, promotions=promotions)
        promoted.append(promoted_step)

    # 4) Outcome-aware validation (boost confianza, no fabricar nada).
    for sp in promoted:
        if sp.type == "open_app":
            if _outcome_validates_open_app(
                mission, (sp.params.get("name") or "").strip(),
            ):
                # Confidence ya es alta del ICE. Solo registramos
                # evidencia de promoción.
                promotions.append({
                    "from": "outcome",
                    "to": "open_app_validated",
                    "name": sp.params.get("name"),
                    "reason": "outcome (window_activate) confirma open_app",
                })
        elif sp.type == "open_site":
            site = (sp.params.get("site") or "").strip()
            if site and _outcome_validates_open_site(mission, site):
                promotions.append({
                    "from": "outcome",
                    "to": "open_site_validated",
                    "site": site,
                    "reason": "outcome confirma open_site (URL/título contiene sitio)",
                })

    # 5) Dedup + orden canónico (antes de UCES/PCOF).
    final_steps = _dedup_and_order(promoted)

    # 5b) UCES / PCOF — promoción primaria entidad-en-colección (compilación).
    plan_pre = SemanticExecutionPlan(
        steps=final_steps,
        source="semantic_intent_promotion_engine",
        version=2,
        coords_used=False,
        legacy_graph_ignored=True,
    )
    try:
        from app.services.runtime.universal_collection_entity_engine import (
            apply_uces_primary_compilation,
        )
        uces_n = apply_uces_primary_compilation(
            plan_pre, mission, promotions=promotions,
        )
        if uces_n:
            final_steps = list(plan_pre.steps)
        # PCOF resolvió entidad pre-transición → quitar ambigüedad legacy.
        if any(
            getattr(s, "type", "") == "select_entity_from_collection"
            and (
                (getattr(s, "params", None) or {}).get("pcof_primary")
                or (getattr(s, "params", None) or {}).get("selection_strategy")
                == "pre_click_operational_freeze"
            )
            for s in final_steps
        ):
            blockers[:] = [
                b for b in blockers
                if b != PromotionBlockerCode.AMBIGUOUS_PROFILE_PICKER
            ]
    except Exception as exc:
        log.debug("[sipe] uces primary compilation: %s", exc)

    # 6) Detectar profile picker ambiguo / query suspect (post-PCOF).
    for sp in final_steps:
        if sp.type == "select_profile":
            profile = (sp.params.get("profile_name")
                       or sp.params.get("profile") or "").strip()
            if not profile or is_generic_profile_label(profile):
                if PromotionBlockerCode.AMBIGUOUS_PROFILE_PICKER not in blockers:
                    blockers.append(PromotionBlockerCode.AMBIGUOUS_PROFILE_PICKER)
        elif sp.type == "search_site":
            fidelity = (sp.params.get("query_fidelity_status") or "ok")
            if fidelity not in ("ok", ""):
                if PromotionBlockerCode.QUERY_FIDELITY_SUSPECT not in blockers:
                    blockers.append(PromotionBlockerCode.QUERY_FIDELITY_SUSPECT)

    # 7) Confianza promedio.
    if final_steps:
        avg = sum(float(s.confidence) for s in final_steps) / float(len(final_steps))
    else:
        avg = 0.0

    plan = SemanticExecutionPlan(
        steps=final_steps,
        source="semantic_intent_promotion_engine",
        version=2,
        average_confidence=round(float(avg), 4),
        coords_used=False,
        legacy_graph_ignored=True,
        intent_status=intent_plan.status,
    )

    try:
        from app.services.missions.legacy_runtime_contamination_guard import (
            filter_legacy_blockers_for_mission_review,
        )
        blockers = filter_legacy_blockers_for_mission_review(blockers, mission)
    except Exception as exc:
        log.debug("[sipe] ffec blocker filter: %s", exc)

    return PromotionResult(
        plan=plan,
        intent_plan=intent_plan,
        promotions=promotions,
        blockers=blockers,
    )


def apply_promotion_to_mission(
    mission: Mission,
    *,
    intent_plan: Optional[MissionIntentPlan] = None,
    persist_legacy_purpose: bool = True,
) -> PromotionResult:
    """Construye el plan promovido y lo persiste en la misión.

    Mutación in-place de ``mission.semantic_execution_plan``. Si
    ``persist_legacy_purpose=True`` (default) y el plan resultante
    tiene al menos un step semántico, marca
    ``mission.legacy_compiled_graph_purpose = "legacy_debug_only"``
    para que el ``smart_runner_bridge`` ignore el grafo legacy.

    También intenta correr ``apply_collapse_to_mission`` para que las
    estructuras intermedias (``mission.compiled_execution_graph``)
    queden coherentes con el nuevo plan — sin romper si esa función
    falla por algún motivo (la misión puede tener un graph legacy
    válido para debug que el ICE no quiere reconstruir).
    """
    # 1. Asegurar que el ICE corrió y dejó coherencia básica antes de
    #    que SIPE prometa intenciones genéricas. apply_collapse_to_mission
    #    es idempotente: si la misión ya viene colapsada, no muta nada.
    if intent_plan is None:
        try:
            apply_collapse_to_mission(mission)
        except Exception as e:
            log.debug(f"[sipe] apply_collapse_to_mission falló (no crítico): {e}")

    result = promote_semantic_intent(mission, intent_plan=intent_plan)

    # 2. Persistir el plan promovido.
    mission.semantic_execution_plan = result.plan.to_dict()

    # 3. Recalcular intent_status / not_ready_reasons / ready_provenance
    #    con la lógica del SEP para que sea consistente con el resto
    #    del pipeline (Mission Review, ApprovalGate, ExecutionDecision).
    attach_intent_status_to_plan(result.plan, mission=mission)
    mission.semantic_execution_plan = result.plan.to_dict()

    # 4. Marcar legacy graph como debug-only cuando hay plan utilizable.
    if persist_legacy_purpose and result.plan.steps:
        try:
            mission.legacy_compiled_graph_purpose = "legacy_debug_only"
        except Exception:
            # Mission es Pydantic — si el campo está bloqueado por
            # config, lo dejamos pasar (no es crítico).
            pass

    # 5. Persistir promociones y blockers en la misión para auditoría
    #    experta post-mortem. PRD 2026-05-14 §SIPE-Persistence: estos
    #    campos son ahora del contrato Mission (Pydantic) y viajan al
    #    store. La UI Mission Review (modo experto) los renderiza.
    try:
        mission.intent_promotions = list(result.promotions)
        mission.intent_promotion_blockers = list(result.blockers)
    except Exception:
        pass

    # 5b. Backwards-compat: algunos callers viejos leen el atributo
    #     privado ``_sipe_promotions`` (era el nombre runtime-only
    #     antes del PRD de persistencia). Mantenemos el alias para no
    #     romperlos. Si Mission tiene ``extra="forbid"`` y bloquea el
    #     setattr, lo ignoramos — el campo persistente ya está.
    try:
        mission._sipe_promotions = list(result.promotions)
        mission._sipe_blockers = list(result.blockers)
    except Exception:
        pass

    try:
        from app.services.missions.operational_truth_preservation import (
            apply_otp_after_promotion,
        )

        apply_otp_after_promotion(mission)
    except Exception as otp_exc:
        log.debug("[sipe] otp after promotion: %s", otp_exc)

    return result


# ─────────────────────────────────────────────────────────────────────
# Helpers públicos
# ─────────────────────────────────────────────────────────────────────


def is_generic_intent_type(intent_type: str) -> bool:
    """¿``intent_type`` es uno de los patrones genéricos del SIPE?"""
    return (intent_type or "").strip().lower() in _GENERIC_INTENT_TYPES


_GENERIC_INTENT_TYPES = frozenset({
    "open_app",
    "select_profile",
    "use_selected_profile",
    "open_new_tab",
    "open_site",
    "search_site",
    "web_search",
    "open_search_result",
    "scroll_results",
    "scroll_page",
})


__all__ = [
    "PromotionResult",
    "PromotionBlockerCode",
    "promote_semantic_intent",
    "apply_promotion_to_mission",
    "is_generic_intent_type",
]
