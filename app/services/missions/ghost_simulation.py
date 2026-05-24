"""
Nevlan — Ghost Simulation (Pre-Save Self-Healing)
==================================================

Antes de guardar una misión como ``EXECUTABLE``, este módulo intenta
**localizar cada target** en la pantalla actual SIN ejecutar nada
(zero side-effect). El objetivo: bloquear misiones rotas antes de
que el usuario las descubra rotas en producción.

Filosofía
---------

    "No guardar misión rota — pero tampoco bloquear intenciones fuertes."

El compiler genera pasos. El approval_gate verifica invariantes
sintácticos. El ghost simulator corre en MODO LECTURA y, cuando NO
encuentra un target en pantalla, antes de pedir confirmación al
usuario, calcula un **Semantic Certainty Score** (PRD 2026-05-06):

  * ``>= 0.85`` → ``semantic_override``: NO pedimos confirmación.
    La intención es lo bastante clara y el executor tiene una cadena
    de resolución robusta (DOM → UIA → OCR → vision → coords).
  * ``0.60–0.84`` → ``soft_warning``: dejamos guardar, mostramos
    una nota no bloqueante "se validará en ejecución".
  * ``< 0.60`` → ``missing`` clásico: pedimos al usuario que
    señale el elemento.

Esto resuelve el problema de Ghost siendo "demasiado conservador":
un ``search_youtube`` con query, site=youtube y locators canónicos
NO debería pedir confirmación sólo porque el campo no estaba
visible en el momento del pre-save (la pestaña podía estar cerrada
o todavía no abierta).

API
---

>>> report = simulate_mission(mission)
>>> if report.needs_user_signal:
>>>     # UI debe mostrar la pregunta por cada step missing
>>>     pass

>>> from app.services.missions.ghost_simulation import semantic_certainty
>>> semantic_certainty(step, context={"site": "youtube"})
0.92

Costo
-----

Pensado para correr en <2s para una misión de ~5 steps. Si
``state_detector`` o ``Playwright`` no están disponibles, los probes
correspondientes devuelven ``uncertain`` y NO bloquean.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    Mission,
)
from app.core.logger import log


# ──────────────────────────────────────────────────────────────────────
# Tipos del reporte
# ──────────────────────────────────────────────────────────────────────

class GhostStatus:
    OK = "ok"                # target encontrado o no necesario
    MISSING = "missing"       # target esperado, NO se encontró
    UNCERTAIN = "uncertain"   # no se pudo verificar (state probe falló)
    SKIPPED = "skipped"       # paso semántico sin target (LAUNCH_APP, etc.)
    # PRD 2026-05-06: target no visible AHORA pero la intención es
    # semánticamente fuerte (search_youtube con query+site+locators,
    # open_new_tab, open_url youtube, scroll_results post-search…).
    # NO bloquea aprobación, NO abre confirmación.
    SEMANTIC_OVERRIDE = "semantic_override"
    # PRD 2026-05-06: certeza media (0.60–0.84). Permite guardar
    # como ejecutable, pero la UI puede pintar una nota no bloqueante.
    SOFT_WARNING = "soft_warning"


# Umbrales del Semantic Certainty Score (PRD 2026-05-06 §1).
SEMANTIC_STRONG_THRESHOLD = 0.85
SEMANTIC_SOFT_THRESHOLD = 0.60


@dataclass
class GhostStepReport:
    step_id: str
    step_index: int
    status: str = GhostStatus.UNCERTAIN
    reason: str = ""
    found_via: str = ""        # "uia_name" | "dom_css" | "ocr" | ...
    suggested_action: str = ""  # "signal" | "rerecord" | "save_at_risk"
    human_label: str = ""
    # Score 0..1 calculado por ``semantic_certainty``. Sólo se rellena
    # cuando el target NO se pudo localizar en pantalla y necesitamos
    # decidir entre missing / soft_warning / semantic_override.
    certainty: float = 0.0
    certainty_reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_index": self.step_index,
            "status": self.status,
            "reason": self.reason,
            "found_via": self.found_via,
            "suggested_action": self.suggested_action,
            "human_label": self.human_label,
            "certainty": round(self.certainty, 3),
            "certainty_reason": self.certainty_reason,
        }


@dataclass
class GhostReport:
    """Resultado de la simulación pre-save de toda una misión."""
    mission_id: str
    steps: List[GhostStepReport] = field(default_factory=list)

    @property
    def missing_steps(self) -> List[GhostStepReport]:
        return [s for s in self.steps if s.status == GhostStatus.MISSING]

    @property
    def uncertain_steps(self) -> List[GhostStepReport]:
        return [s for s in self.steps if s.status == GhostStatus.UNCERTAIN]

    @property
    def ok_steps(self) -> List[GhostStepReport]:
        return [s for s in self.steps if s.status == GhostStatus.OK]

    @property
    def needs_user_signal(self) -> bool:
        """True si hay al menos UN step MISSING — el usuario debe
        señalar/regrabar antes de poder guardar como EXECUTABLE."""
        return bool(self.missing_steps)

    @property
    def total(self) -> int:
        return len(self.steps)

    def summary(self) -> Dict[str, int]:
        out = {
            "ok": 0,
            "missing": 0,
            "uncertain": 0,
            "skipped": 0,
            "semantic_override": 0,
            "soft_warning": 0,
        }
        for s in self.steps:
            out[s.status] = out.get(s.status, 0) + 1
        return out

    @property
    def semantic_override_steps(self) -> List["GhostStepReport"]:
        return [s for s in self.steps if s.status == GhostStatus.SEMANTIC_OVERRIDE]

    @property
    def soft_warning_steps(self) -> List["GhostStepReport"]:
        return [s for s in self.steps if s.status == GhostStatus.SOFT_WARNING]


# ──────────────────────────────────────────────────────────────────────
# Probes individuales
# ──────────────────────────────────────────────────────────────────────

def _human_label(cs: CompiledStep) -> str:
    """Texto humano del step para mensajes de UI."""
    sem = (cs.target_context.semantic if cs.target_context else None) or {}
    if sem.get("human_label"):
        return str(sem["human_label"])
    pl = cs.action_payload or {}
    if cs.action_strategy == ActionStrategy.LAUNCH_APP:
        return f"Abrir {pl.get('app_name') or pl.get('app') or 'app'}"
    if cs.action_strategy == ActionStrategy.SELECT_PROFILE:
        return f"Perfil {pl.get('profile') or '(?)'}"
    if cs.action_strategy == ActionStrategy.OPEN_NEW_TAB:
        return "Nueva pestaña"
    if cs.action_strategy == ActionStrategy.OPEN_BOOKMARK:
        return f"Bookmark {pl.get('name') or pl.get('url')}"
    if cs.action_strategy in (
        ActionStrategy.SUBMIT_SEARCH,
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        site = pl.get("site") or pl.get("search_site") or ""
        if site:
            return f"Buscar en {site}: {pl.get('text', '')}"
        return f"Escribir: {pl.get('text', '')}"
    if cs.action_strategy == ActionStrategy.NAVIGATE_OR_SEARCH:
        return f"Ir a {pl.get('text') or pl.get('url') or '(?)'}"
    return cs.goal or cs.action_strategy.value


def _is_target_optional(cs: CompiledStep) -> bool:
    """Pasos cuyo "target" no se valida visualmente (se ejecutan por
    nombre/intent). Coincide con ``_NON_VISUAL_ACTIONS`` del approval_gate.
    """
    return cs.action_strategy in {
        ActionStrategy.LAUNCH_APP,
        ActionStrategy.NAVIGATE_OR_SEARCH,
        ActionStrategy.OPEN_NEW_TAB,
        ActionStrategy.SEND_HOTKEY,
        ActionStrategy.WAIT_FOR_STATE,
        ActionStrategy.AGENT_PROMPT,
    }


def _probe_uia_by_name(name: str) -> Optional[str]:
    """Busca un control UIA con ``Name`` igual o similar. Devuelve
    "uia_name" si lo encuentra, None si no o si UIA no está disponible.
    """
    if not name:
        return None
    try:
        import uiautomation as auto  # type: ignore
    except Exception:
        return None
    try:
        # Búsqueda no exhaustiva: tree walker desde root con Cache=False.
        # Se aborta tras N nodos para no bloquear el guardado.
        root = auto.GetRootControl()
        N_MAX = 1500
        count = 0
        target_lower = name.lower().strip()
        for ctrl, _depth in _walk_uia(root):
            count += 1
            if count > N_MAX:
                return None
            try:
                ctrl_name = (ctrl.Name or "").strip().lower()
            except Exception:
                continue
            if ctrl_name == target_lower:
                return "uia_name"
            if target_lower in ctrl_name and len(target_lower) >= 3:
                return "uia_name_partial"
        return None
    except Exception as e:
        log.debug(f"[ghost] uia probe failed: {e}")
        return None


def _walk_uia(node, depth: int = 0):
    """Generador defensivo del árbol UIA. Profundidad máxima 6 para
    evitar bombas combinatorias."""
    if depth > 6:
        return
    try:
        yield node, depth
    except Exception:
        return
    try:
        children = node.GetChildren()
    except Exception:
        return
    for c in children or []:
        yield from _walk_uia(c, depth + 1)


def _probe_dom_locator(locators: List[Dict[str, Any]]) -> Optional[str]:
    """Probar locators DOM con Playwright (si Chrome está conectado).
    No abre browser propio: solo se conecta si ya existe.
    """
    if not locators:
        return None
    try:
        from app.skills.tools import web as web_tools  # type: ignore
        page_fn = getattr(web_tools, "_get_page", None)
        if not callable(page_fn):
            return None
        page = page_fn(connect_only=True)
        if page is None:
            return None
    except Exception:
        return None
    for loc in locators:
        kind = str(loc.get("kind") or "").lower()
        val = str(loc.get("value") or loc.get("selector") or "").strip()
        if not val:
            continue
        try:
            if kind in {"css", "css_selector"}:
                el = page.locator(val).first
                if el.count() > 0:
                    return "dom_css"
            if kind in {"aria_label", "aria-label"}:
                el = page.get_by_label(val).first
                if el.count() > 0:
                    return "dom_aria"
            if kind == "test_id":
                el = page.get_by_test_id(val).first
                if el.count() > 0:
                    return "dom_test_id"
        except Exception as e:
            log.debug(f"[ghost] dom probe '{kind}={val}': {e}")
            continue
    return None


def _probe_browser_url_match(expected_url_substr: str) -> Optional[str]:
    """¿La URL actual del browser activo contiene el substring esperado?"""
    if not expected_url_substr:
        return None
    try:
        from app.services.missions.state_detector import detect_state
    except Exception:
        return None
    try:
        snap = detect_state(quick=True)
    except Exception:
        return None
    url = (getattr(snap, "browser_url", None) or "").lower()
    if expected_url_substr.lower() in url:
        return "browser_url"
    return None


def _probe_running_process(name: str) -> Optional[str]:
    """¿Está corriendo el proceso ``name``? (Para LAUNCH_APP y
    SELECT_PROFILE: si la app no corre, el step seguro arranca con
    open_app — pero el estado actual no significa fallo).
    """
    if not name:
        return None
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        for p in psutil.process_iter(attrs=["name"]):
            n = (p.info.get("name") or "").lower()
            if name.lower() in n:
                return "process"
        return None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# Semantic Certainty Score (PRD 2026-05-06 §1)
# ──────────────────────────────────────────────────────────────────────
#
# El score evalúa cuán "obvia" es la intención de un step más allá
# de su localización visual. Un score alto significa que el executor
# tendrá formas robustas de resolver el target en runtime aunque
# Ghost no lo encuentre AHORA.
#
# Inputs por step:
#   - step.action_strategy (type)
#   - step.action_payload (params obligatorios)
#   - step.target_context.semantic (intent, human_label, site…)
#   - mission_context (window/title/url/process detectados, eventos
#     posteriores como Enter, raw_trace si está)
#
# Pesos (suman ≤ 1.0; clipeado a [0, 1]):
#   - tipo conocido + payload obligatorio completo .................. 0.30
#   - app/domain/window context coherente ........................... 0.20
#   - evento posterior compatible (Enter, submit) ................... 0.10
#   - evidencia de intención (semantic.intent / human_label) ........ 0.10
#   - cadena de resolución robusta disponible (locators / hotkey) ... 0.30
#
# Categorías:
#   - search_youtube: query + site=youtube + target=youtube_search_box
#     + locators canónicos → 0.90+
#   - open_new_tab on chrome: hotkey Ctrl+T → 0.95
#   - open_url youtube: Ctrl+L + url → 0.92
#   - scroll_results post-search: scroll dinámico → 0.90
#   - select_profile sin nombre: 0.30 (pedimos confirmación)
#   - click genérico sin name/locator: 0.20

# Tokens YouTube usados en heurísticas de contexto.
_YT_HINTS = ("youtube", "youtu.be")


def _safe_str(v: Any) -> str:
    try:
        return str(v or "").strip()
    except Exception:
        return ""


def _semantic_dict(cs: CompiledStep) -> Dict[str, Any]:
    sem = cs.target_context.semantic if cs.target_context else None
    return dict(sem or {})


def _payload(cs: CompiledStep) -> Dict[str, Any]:
    return dict(cs.action_payload or {})


def _has_youtube_context(cs: CompiledStep, ctx: Dict[str, Any]) -> bool:
    """¿Hay evidencia clara de YouTube en payload, semantic o ctx?"""
    pl = _payload(cs)
    sem = _semantic_dict(cs)
    candidates: List[str] = []
    for d in (pl, sem):
        for k in ("site", "search_site", "domain", "url", "name"):
            v = _safe_str(d.get(k)).lower()
            if v:
                candidates.append(v)
    for k in ("title", "url", "domain", "process_name", "block_title"):
        v = _safe_str(ctx.get(k)).lower()
        if v:
            candidates.append(v)
    blob = " | ".join(candidates)
    return any(h in blob for h in _YT_HINTS)


def _has_chrome_context(cs: CompiledStep, ctx: Dict[str, Any]) -> bool:
    """¿La app/proc/title sugiere que estamos hablando de Chrome?"""
    pl = _payload(cs)
    bag = " ".join(
        _safe_str(x) for x in (
            pl.get("app"), pl.get("app_name"), pl.get("name"),
            ctx.get("app"), ctx.get("process_name"),
            ctx.get("window_title"), ctx.get("title"),
        )
    ).lower()
    return "chrome" in bag


def _had_enter_after(cs: CompiledStep, ctx: Dict[str, Any]) -> bool:
    """¿El raw_trace o el payload demuestran que se pulsó Enter
    después del texto?"""
    pl = _payload(cs)
    if pl.get("submit_after") or pl.get("submit"):
        return True
    sem = _semantic_dict(cs)
    if sem.get("submit_after") or sem.get("submit"):
        return True
    raw = ctx.get("raw_trace_after_keys") or ctx.get("post_keys") or []
    try:
        keys = " ".join(_safe_str(k).lower() for k in raw)
    except Exception:
        keys = ""
    return "enter" in keys


def _has_robust_resolution_chain(cs: CompiledStep) -> bool:
    """¿El step trae locators/intent que el executor puede resolver
    sin necesidad de bbox visual previo?

    Casos cubiertos:
      * SUBMIT_SEARCH/SET_FIELD_VALUE/TYPE_TEXT con ``locators`` o
        ``site`` conocido → DOM/UIA/OCR.
      * OPEN_NEW_TAB → hotkey Ctrl+T (no requiere target visual).
      * NAVIGATE_OR_SEARCH/OPEN_BOOKMARK/OPEN_URL → Ctrl+L + url.
      * SCROLL_UNTIL_VISIBLE → wheel/JS/keyboard, sin target exacto.
      * LAUNCH_APP/SEND_HOTKEY/WAIT_FOR_STATE → no requieren target.
    """
    a = cs.action_strategy
    pl = _payload(cs)
    web = (cs.target_context.web_data if cs.target_context else None) or {}
    if a in (
        ActionStrategy.OPEN_NEW_TAB,
        ActionStrategy.LAUNCH_APP,
        ActionStrategy.SEND_HOTKEY,
        ActionStrategy.WAIT_FOR_STATE,
        ActionStrategy.NAVIGATE_OR_SEARCH,
        ActionStrategy.OPEN_BOOKMARK,
        ActionStrategy.SCROLL_UNTIL_VISIBLE,
    ):
        return True
    if a in (
        ActionStrategy.SUBMIT_SEARCH,
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        if pl.get("locators") or web.get("locators"):
            return True
        site = _safe_str(pl.get("site") or pl.get("search_site")).lower()
        target_label = _safe_str(
            pl.get("target_label") or pl.get("target")
        ).lower()
        # Sites conocidos con cadena de resolución canónica (DOM + UIA + OCR).
        if site in {"youtube", "google", "bing", "duckduckgo"}:
            return True
        if "search" in target_label or "buscar" in target_label:
            return True
    return False


def _required_params_complete(cs: CompiledStep) -> bool:
    """¿Los parámetros obligatorios para ejecutar la intención están
    presentes? (No exige los visuales — sólo los semánticos.)
    """
    a = cs.action_strategy
    pl = _payload(cs)
    if a in (
        ActionStrategy.SUBMIT_SEARCH,
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        return bool(_safe_str(pl.get("text") or pl.get("query") or pl.get("val")))
    if a == ActionStrategy.LAUNCH_APP:
        return bool(_safe_str(pl.get("app_name") or pl.get("app")))
    if a == ActionStrategy.OPEN_NEW_TAB:
        # Ctrl+T no necesita params extra: con la app context basta.
        return True
    if a == ActionStrategy.NAVIGATE_OR_SEARCH:
        return bool(_safe_str(pl.get("url") or pl.get("text") or pl.get("name")))
    if a == ActionStrategy.OPEN_BOOKMARK:
        return bool(_safe_str(pl.get("url") or pl.get("name")))
    if a == ActionStrategy.SELECT_PROFILE:
        return bool(_safe_str(pl.get("profile") or pl.get("user_confirmed_label")))
    if a == ActionStrategy.SEND_HOTKEY:
        return bool(pl.get("keys") or pl.get("hotkey") or pl.get("key"))
    if a == ActionStrategy.SCROLL_UNTIL_VISIBLE:
        return True
    if a in (ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK,
             ActionStrategy.RIGHT_CLICK):
        sem = _semantic_dict(cs)
        uia = (cs.target_context.uia_data if cs.target_context else None) or {}
        web = (cs.target_context.web_data if cs.target_context else None) or {}
        return bool(
            sem.get("human_label") or uia.get("name")
            or uia.get("automation_id") or web.get("locators")
        )
    return True


def semantic_certainty(
    step: CompiledStep,
    context: Optional[Dict[str, Any]] = None,
) -> float:
    """Calcula el Semantic Certainty Score [0..1] del step.

    El score responde a la pregunta: "Si Ghost NO encontró el target
    visual ahora, ¿qué tan seguros estamos de que el executor podrá
    resolverlo en runtime?".

    Reglas (PRD 2026-05-06 §1):

      * tipo conocido + params obligatorios completos          (+0.30)
      * app/domain/window context coherente                    (+0.20)
      * evento posterior compatible (Enter/submit)             (+0.10)
      * evidencia de intención (semantic.intent / human_label) (+0.10)
      * cadena de resolución robusta disponible                (+0.30)

    El cap es 1.0; el suelo 0.0. Casos especiales (search_youtube,
    open_new_tab, open_url youtube, scroll_results) reciben un
    "boost" determinista cuando se cumplen sus condiciones.

    ``context`` es opcional. Cuando se aporta, debe contener al
    menos algún subset de ``{title, url, domain, process_name,
    app, raw_trace_after_keys, post_keys, block_title}`` derivado
    del estado actual o del bloque semántico que contiene al step.
    """
    if step is None:
        return 0.0
    ctx: Dict[str, Any] = dict(context or {})
    score = 0.0
    a = step.action_strategy
    sem = _semantic_dict(step)
    pl = _payload(step)

    # 1) Tipo conocido + params completos.
    if isinstance(a, ActionStrategy) and _required_params_complete(step):
        score += 0.30

    # 2) Contexto coherente.
    has_app_ctx = bool(
        _safe_str(pl.get("app") or pl.get("app_name"))
        or ctx.get("app") or ctx.get("process_name")
        or ctx.get("title") or ctx.get("window_title")
    )
    has_domain_ctx = (
        _has_youtube_context(step, ctx)
        or _has_chrome_context(step, ctx)
        or bool(_safe_str(pl.get("site") or pl.get("search_site")))
    )
    if has_app_ctx or has_domain_ctx:
        score += 0.20

    # 3) Evento posterior compatible.
    if _had_enter_after(step, ctx):
        score += 0.10

    # 4) Evidencia de intención.
    if _safe_str(sem.get("intent")) or _safe_str(sem.get("human_label")):
        score += 0.10

    # 5) Cadena de resolución robusta.
    if _has_robust_resolution_chain(step):
        score += 0.30

    # ── Boost determinista por intención (PRD §3) ─────────────────────
    # Estos boosts son aditivos pero clipeamos el resultado a 1.0.
    intent_boost, _intent_reason = _intent_specific_certainty(step, ctx)
    if intent_boost > 0:
        score = max(score, intent_boost)

    # Penalización: si el step lleva un placeholder humano sin resolver
    # (profile vacío, target_label vacío en search), el score baja.
    if a == ActionStrategy.SELECT_PROFILE \
            and not _safe_str(pl.get("profile") or pl.get("user_confirmed_label")):
        score = min(score, 0.40)

    return max(0.0, min(1.0, round(score, 3)))


def _intent_specific_certainty(
    step: CompiledStep, ctx: Dict[str, Any],
) -> tuple[float, str]:
    """Devuelve (score_minimo_garantizado, reason) para casos donde la
    intención es tan obvia que el score debe rebasar 0.85 sin dudar
    (PRD 2026-05-06 §3).

    No reemplaza la suma del score base; sólo levanta el piso cuando
    se cumplen TODAS las condiciones de la regla.
    """
    a = step.action_strategy
    pl = _payload(step)
    sem = _semantic_dict(step)
    intent = _safe_str(sem.get("intent")).lower()

    # A) search_youtube: query + site/youtube context + (submit OR target hint).
    if a in (
        ActionStrategy.SUBMIT_SEARCH,
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        query = _safe_str(pl.get("text") or pl.get("query") or pl.get("val"))
        target_label = _safe_str(
            pl.get("target_label") or pl.get("target")
        ).lower()
        looks_like_youtube_box = (
            target_label in {"youtube_search_box", "search_query"}
            or "youtube" in target_label
        )
        if (
            query
            and (_has_youtube_context(step, ctx) or intent == "search_youtube")
            and (
                _had_enter_after(step, ctx)
                or looks_like_youtube_box
                or pl.get("locators")
            )
        ):
            return 0.92, "search_youtube semantically obvious"

    # B) open_new_tab on chrome: Ctrl+T no requiere visual.
    if a == ActionStrategy.OPEN_NEW_TAB:
        if _has_chrome_context(step, ctx) or intent == "open_new_tab":
            return 0.95, "open_new_tab via Ctrl+T"
        # Sin contexto explícito, asumimos browser → still strong.
        return 0.88, "open_new_tab generic"

    # C) open_url / open_bookmark / navigate hacia youtube.
    if a in (
        ActionStrategy.OPEN_BOOKMARK,
        ActionStrategy.NAVIGATE_OR_SEARCH,
    ):
        url = _safe_str(pl.get("url") or pl.get("name") or pl.get("text")).lower()
        if "youtube" in url or _has_youtube_context(step, ctx):
            return 0.92, "open_url youtube via Ctrl+L"
        if url:
            # URL conocida pero no youtube → sigue siendo intención fuerte.
            return 0.88, "open_url with known target"

    # D) scroll_results después de un search.
    if a == ActionStrategy.SCROLL_UNTIL_VISIBLE or intent == "scroll_results":
        ctx_blob = " ".join(
            _safe_str(ctx.get(k)).lower()
            for k in ("prev_intent", "block_title", "after_intent")
        )
        if (
            "search" in ctx_blob or "youtube_results" in ctx_blob
            or _has_youtube_context(step, ctx)
        ):
            return 0.90, "scroll_results after search"
        return 0.85, "scroll_results dinámico"

    # E) select_profile: sólo es fuerte si trae profile o confirmación.
    if a == ActionStrategy.SELECT_PROFILE:
        profile = _safe_str(pl.get("profile") or pl.get("user_confirmed_label"))
        if profile:
            return 0.90, f"select_profile profile={profile}"

    # F) launch_app / hotkey / wait_for_state: no requieren visual.
    if a in (
        ActionStrategy.LAUNCH_APP,
        ActionStrategy.SEND_HOTKEY,
        ActionStrategy.WAIT_FOR_STATE,
    ):
        if _required_params_complete(step):
            return 0.90, f"{a.value} sin target visual"

    return 0.0, ""


def _build_step_context(
    cs: CompiledStep,
    mission: Optional[Mission] = None,
    idx: int = 0,
) -> Dict[str, Any]:
    """Construye un dict ``context`` para ``semantic_certainty`` a
    partir de la misión: detecta intent del step previo, recoge raw
    keys post-step y exporta hints del action_group si existen.
    """
    ctx: Dict[str, Any] = {}
    if mission is None:
        return ctx
    graph = mission.compiled_execution_graph or []
    if idx > 0 and idx - 1 < len(graph):
        prev = graph[idx - 1]
        prev_sem = _semantic_dict(prev)
        ctx["prev_intent"] = (
            _safe_str(prev_sem.get("intent")) or prev.action_strategy.value
        )
    # Buscamos el ActionGroup que contiene este step (block_title hint).
    for grp in mission.action_groups or []:
        if cs.id in (grp.step_ids or []):
            ctx["block_title"] = _safe_str(grp.group_title)
            break
    # Window/process detectado en el TargetContext.
    tc = cs.target_context
    if tc is not None:
        ctx["process_name"] = _safe_str(tc.process_name)
        ctx["window_title"] = _safe_str(tc.window_title)
        web = tc.web_data or {}
        if isinstance(web, dict):
            ctx["url"] = _safe_str(web.get("url"))
            ctx["domain"] = _safe_str(web.get("domain"))
    # Detectar si hubo Enter en el raw_trace tras el último timestamp
    # del step (heurística simple: cualquier KEYBOARD_HOTKEY con "enter"
    # en raw_trace es señal de submit).
    try:
        post_keys: List[str] = []
        for ev in mission.raw_trace or []:
            ka = getattr(ev, "keyboard_action", None)
            if not ka:
                continue
            for k in (ka.keys or []) + (ka.modifiers or []):
                ks = _safe_str(k).lower()
                if ks == "enter":
                    post_keys.append(ks)
                    break
        if post_keys:
            ctx["raw_trace_after_keys"] = post_keys
    except Exception:
        pass
    return ctx


def _decide_status_for_missing(
    cs: CompiledStep,
    rep: GhostStepReport,
    mission: Optional[Mission],
    idx: int,
    miss_reason: str,
) -> None:
    """Aplica la Ghost Decision Policy (PRD 2026-05-06 §2):

      * certainty >= SEMANTIC_STRONG_THRESHOLD → SEMANTIC_OVERRIDE
      * SEMANTIC_SOFT_THRESHOLD <= certainty < STRONG → SOFT_WARNING
      * certainty < SOFT_THRESHOLD → MISSING (pedimos confirmación)
    """
    ctx = _build_step_context(cs, mission, idx)
    score = semantic_certainty(cs, ctx)
    rep.certainty = score
    if score >= SEMANTIC_STRONG_THRESHOLD:
        rep.status = GhostStatus.SEMANTIC_OVERRIDE
        rep.reason = (
            f"target no visible ahora pero intención fuerte "
            f"(certainty={score:.2f}); el executor lo resolverá en runtime"
        )
        rep.certainty_reason = "semantic_strong"
        rep.suggested_action = ""
        return
    if score >= SEMANTIC_SOFT_THRESHOLD:
        rep.status = GhostStatus.SOFT_WARNING
        rep.reason = (
            f"target no visible ahora; certeza media "
            f"(certainty={score:.2f}); se validará durante ejecución"
        )
        rep.certainty_reason = "semantic_soft"
        rep.suggested_action = "save_at_risk"
        return
    rep.status = GhostStatus.MISSING
    rep.reason = miss_reason
    rep.certainty_reason = "semantic_weak"
    rep.suggested_action = "signal"


# ──────────────────────────────────────────────────────────────────────
# Simulación por step
# ──────────────────────────────────────────────────────────────────────

def _simulate_step(
    cs: CompiledStep,
    idx: int,
    mission: Optional[Mission] = None,
) -> GhostStepReport:
    rep = GhostStepReport(
        step_id=cs.id,
        step_index=idx,
        human_label=_human_label(cs),
    )

    # Pasos semánticos que NO requieren target visual: skipped → OK.
    # Confiamos en su intención (ver "Confidence Policy V2").
    if _is_target_optional(cs):
        rep.status = GhostStatus.OK
        rep.reason = "step semántico sin target visual"
        return rep

    # Strategy-specific probes:
    a = cs.action_strategy
    pl = cs.action_payload or {}
    sem = (cs.target_context.semantic if cs.target_context else None) or {}
    web = (cs.target_context.web_data if cs.target_context else None) or {}
    uia = (cs.target_context.uia_data if cs.target_context else None) or {}

    # 1) SET_FIELD_VALUE / SUBMIT_SEARCH / TYPE_TEXT con site → DOM probe.
    site = str(pl.get("site") or pl.get("search_site") or "").lower()
    if a in (
        ActionStrategy.SUBMIT_SEARCH,
        ActionStrategy.SET_FIELD_VALUE,
        ActionStrategy.TYPE_TEXT,
    ):
        # Si tenemos site youtube/google y la URL actual coincide,
        # el campo está disponible (la vista del browser lo muestra).
        url_hit = _probe_browser_url_match("youtube.com" if site == "youtube" else site)
        if url_hit:
            rep.status = GhostStatus.OK
            rep.found_via = url_hit
            return rep
        # Probar locators DOM si los tenemos.
        locators = list(web.get("locators") or [])
        dom_hit = _probe_dom_locator(locators)
        if dom_hit:
            rep.status = GhostStatus.OK
            rep.found_via = dom_hit
            return rep
        # Fallback UIA.
        target_label = str(
            pl.get("target_label") or pl.get("target") or sem.get("human_label") or ""
        )
        uia_hit = _probe_uia_by_name(target_label)
        if uia_hit:
            rep.status = GhostStatus.OK
            rep.found_via = uia_hit
            return rep
        # No encontrado: solo missing si el usuario está en la app
        # esperada. Si el browser ni corre, es UNCERTAIN (necesita
        # ejecutar steps anteriores primero).
        if not _probe_running_process("chrome.exe"):
            # Sin Chrome corriendo el ghost no puede pronunciarse.
            # Antes de devolver UNCERTAIN, pasamos por la decision
            # policy: una intención muy fuerte (search_youtube con
            # query+site+locators) merece semantic_override aunque
            # ni siquiera podamos probar visualmente.
            _decide_status_for_missing(
                cs, rep, mission, idx,
                "Chrome no está corriendo aún",
            )
            if rep.status == GhostStatus.MISSING:
                # No subimos la certeza → degradamos a UNCERTAIN
                # para preservar el comportamiento histórico (no
                # bloquear cuando ni siquiera se pudo probar).
                rep.status = GhostStatus.UNCERTAIN
                rep.reason = "Chrome no está corriendo aún"
            return rep
        _decide_status_for_missing(
            cs, rep, mission, idx,
            "barra de búsqueda no encontrada",
        )
        return rep

    # 2) OPEN_BOOKMARK → ¿la URL existe en bookmarks? No la podemos
    #    verificar sin permisos — UNCERTAIN benigno.
    if a == ActionStrategy.OPEN_BOOKMARK:
        rep.status = GhostStatus.UNCERTAIN
        rep.reason = "no se verifica bookmarks sin permisos"
        return rep

    # 3) SELECT_PROFILE → ¿está la pantalla de profile picker?
    if a == ActionStrategy.SELECT_PROFILE:
        rep.status = GhostStatus.UNCERTAIN
        rep.reason = "el picker aparece tras open_app; se verifica en runtime"
        return rep

    # 4) CLICK / DOUBLE_CLICK / RIGHT_CLICK → buscar por nombre UIA.
    if a in (
        ActionStrategy.CLICK,
        ActionStrategy.DOUBLE_CLICK,
        ActionStrategy.RIGHT_CLICK,
    ):
        name = str(uia.get("name") or sem.get("human_label") or "").strip()
        if name:
            hit = _probe_uia_by_name(name)
            if hit:
                rep.status = GhostStatus.OK
                rep.found_via = hit
                return rep
        # Locators DOM si están.
        locators = list(web.get("locators") or [])
        if locators:
            dom_hit = _probe_dom_locator(locators)
            if dom_hit:
                rep.status = GhostStatus.OK
                rep.found_via = dom_hit
                return rep
        _decide_status_for_missing(
            cs, rep, mission, idx,
            f"target '{name or '(sin nombre)'}' no visible",
        )
        return rep

    # 5) SCROLL_UNTIL_VISIBLE → no aplicable (su target lo busca el
    #    runtime durante la ejecución).
    if a == ActionStrategy.SCROLL_UNTIL_VISIBLE:
        rep.status = GhostStatus.OK
        rep.reason = "scroll dinámico, sin target estático"
        return rep

    # Default: skipped.
    rep.status = GhostStatus.SKIPPED
    rep.reason = f"sin probe definido para {a.value}"
    return rep


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def simulate_mission(mission: Mission) -> GhostReport:
    """Corre el ghost simulator sobre todos los pasos de la misión.

    PRD 2026-05-07c §G: cuando la misión tiene un
    ``semantic_execution_plan`` válido, **simulamos contra el plan
    semántico** (intención limpia), no contra el ``compiled_execution_graph``
    legacy (que arrastra clicks crudos, GroupControl, etc.).

    Reglas de fallo del ghost contra el plan semántico:
      * profile_name vacío → MISSING (perfil obligatorio).
      * query vacía → MISSING.
      * query_fidelity_status != "ok" → MISSING (con SOFT_WARNING si
        Mission Review ya lo confirmó).
      * type/strategy contaminado con coords/legacy → MISSING.
      * open_new_tab sin Ctrl+T / open_url sin Ctrl+L → MISSING.
    """
    rep = GhostReport(mission_id=mission.id if mission else "")
    if not mission:
        return rep

    # Modo semántico (preferido).
    sep = getattr(mission, "semantic_execution_plan", None)
    if isinstance(sep, dict) and (sep.get("steps") or []):
        rep = _simulate_semantic_plan(mission, sep)
        log.info(
            f"[ghost] mission={mission.id} mode=semantic_plan "
            f"summary={rep.summary()} needs_signal={rep.needs_user_signal}"
        )
        return rep

    # Modo legacy: solo si NO hay plan semántico.
    if not mission.compiled_execution_graph:
        return rep
    for i, cs in enumerate(mission.compiled_execution_graph):
        try:
            sr = _simulate_step(cs, i, mission)
        except Exception as e:
            sr = GhostStepReport(
                step_id=cs.id, step_index=i,
                status=GhostStatus.UNCERTAIN,
                reason=f"probe error: {e}",
                human_label=_human_label(cs),
            )
        rep.steps.append(sr)
    log.info(
        f"[ghost] mission={mission.id} mode=legacy_graph "
        f"summary={rep.summary()} needs_signal={rep.needs_user_signal}"
    )
    return rep


# ──────────────────────────────────────────────────────────────────────
# Simulación contra semantic_execution_plan (PRD 2026-05-07c §G)
# ──────────────────────────────────────────────────────────────────────


def _simulate_semantic_plan(
    mission: Mission, sep: Dict[str, Any],
) -> "GhostReport":
    """Simula la ejecución del semantic_execution_plan en modo lectura.

    A diferencia del ghost legacy (que prueba targets visuales), este
    valida la **intención** declarada por el plan: cada step debe
    tener sus parámetros mínimos completos y su preferred_strategy
    en el set canónico (no coords como primaria).
    """
    rep = GhostReport(mission_id=mission.id if mission else "")
    steps = sep.get("steps") or []
    coords_used = bool(sep.get("coords_used"))
    legacy_ignored = bool(sep.get("legacy_graph_ignored"))

    # Pre-flight: si el plan ya declara coords_used o no ignora legacy,
    # toda la simulación debe fallar con MISSING (no es ejecutable).
    plan_level_problem = ""
    if coords_used:
        plan_level_problem = "plan declara coords_used=true"
    elif not legacy_ignored:
        plan_level_problem = "plan no ignora compiled_execution_graph legacy"

    for i, ps in enumerate(steps):
        if not isinstance(ps, dict):
            sr = GhostStepReport(
                step_id=f"plan:{i}",
                step_index=i,
                status=GhostStatus.MISSING,
                reason=f"step #{i} no es dict",
                human_label=f"step #{i}",
                certainty=0.0,
                certainty_reason="non_dict_step",
                suggested_action="rerecord",
            )
            rep.steps.append(sr)
            continue
        ptype = str(ps.get("type") or "")
        params = ps.get("params") or {}
        sid = str(ps.get("id") or f"plan:{i}:{ptype}")
        human_label = str(ps.get("human_label") or ptype)
        primary = str(ps.get("preferred_strategy") or "").lower()

        sr = GhostStepReport(
            step_id=sid,
            step_index=i,
            status=GhostStatus.OK,
            reason="step semántico válido",
            human_label=human_label,
            certainty=float(ps.get("confidence") or 0.0),
        )

        if plan_level_problem:
            sr.status = GhostStatus.MISSING
            sr.reason = plan_level_problem
            sr.suggested_action = "save_at_risk"
            rep.steps.append(sr)
            continue

        if ps.get("needs_user_label"):
            sr.status = GhostStatus.MISSING
            sr.reason = (
                ps.get("label_prompt")
                or f"step '{ptype}' necesita confirmación humana"
            )
            sr.suggested_action = "signal"
            rep.steps.append(sr)
            continue

        if ptype == "select_profile":
            profile = str(
                params.get("profile_name") or params.get("profile") or ""
            ).strip()
            if not profile:
                sr.status = GhostStatus.MISSING
                sr.reason = "profile_name vacío en select_profile"
                sr.suggested_action = "signal"
        elif ptype == "search_youtube":
            query = str(params.get("query") or "").strip()
            fidelity = str(params.get("query_fidelity_status") or "ok")
            if not query:
                sr.status = GhostStatus.MISSING
                sr.reason = "query vacía en search_youtube"
                sr.suggested_action = "signal"
            elif fidelity == "missing_short_token":
                sr.status = GhostStatus.MISSING
                sr.reason = "query con pérdida sospechosa de token corto"
                sr.suggested_action = "signal"
            elif "coord" in primary or "visual" in primary:
                sr.status = GhostStatus.MISSING
                sr.reason = (
                    f"search_youtube depende de coords/visual ({primary})"
                )
                sr.suggested_action = "signal"
        elif ptype == "open_new_tab":
            if primary != "open_new_tab:hotkey_ctrl_t":
                sr.status = GhostStatus.MISSING
                sr.reason = (
                    "open_new_tab no usa Ctrl+T como estrategia primaria"
                )
                sr.suggested_action = "signal"
        elif ptype == "open_url":
            if not (params.get("url") or params.get("alias")):
                sr.status = GhostStatus.MISSING
                sr.reason = "open_url sin url ni alias"
                sr.suggested_action = "signal"
            elif primary != "open_url:hotkey_ctrl_l":
                sr.status = GhostStatus.MISSING
                sr.reason = (
                    "open_url no usa Ctrl+L como estrategia primaria"
                )
                sr.suggested_action = "signal"
        elif ptype == "open_app":
            if not params.get("name"):
                sr.status = GhostStatus.MISSING
                sr.reason = "open_app sin name"
                sr.suggested_action = "signal"
        elif ptype == "scroll_results":
            # scroll dinámico — siempre OK.
            pass
        elif ptype not in ("wait_for", "confirm"):
            sr.status = GhostStatus.MISSING
            sr.reason = f"tipo desconocido: {ptype}"
            sr.suggested_action = "signal"

        rep.steps.append(sr)
    return rep


def mark_missing_for_review(mission: Mission, report: GhostReport) -> int:
    """Tras una simulación, escribe en cada step:

      * ``ghost_status`` con uno de los valores de :class:`GhostStatus`.
      * ``ghost_certainty`` con el score 0..1 que produjo la decisión.
      * ``ghost_reason`` informativo.

    Y, sólo cuando ``status == MISSING``:

      * ``needs_user_label = True``
      * ``ghost_missing = True``
      * ``label_prompt`` con la pregunta para el usuario.

    Cuando ``status == SEMANTIC_OVERRIDE`` (PRD 2026-05-06 §2 caso A)
    NO marcamos ``needs_user_label`` ni ``ghost_missing``. El step
    queda ``executable=True``. Cuando ``status == SOFT_WARNING`` (caso
    B) marcamos ``ghost_warning`` no bloqueante.

    Devuelve el nº de steps marcados como ``ghost_missing`` (sólo los
    que requieren intervención humana).
    """
    if not mission or not mission.compiled_execution_graph or not report:
        return 0
    by_id = {sr.step_id: sr for sr in report.steps}
    n_missing = 0
    for cs in mission.compiled_execution_graph:
        sr = by_id.get(cs.id)
        if sr is None:
            continue
        pl = dict(cs.action_payload or {})
        # Limpieza defensiva: si el step venía marcado por una
        # ejecución anterior, el resultado nuevo manda.
        for k in ("ghost_missing", "ghost_warning", "ghost_status",
                  "ghost_certainty", "ghost_reason"):
            pl.pop(k, None)

        if sr.status == GhostStatus.SEMANTIC_OVERRIDE:
            pl["ghost_status"] = GhostStatus.SEMANTIC_OVERRIDE
            pl["ghost_certainty"] = round(sr.certainty, 3)
            pl["ghost_warning"] = "target_not_visible_now_but_semantics_strong"
            pl["ghost_reason"] = sr.reason
            pl["executable"] = True
            cs.action_payload = pl
            continue

        if sr.status == GhostStatus.SOFT_WARNING:
            pl["ghost_status"] = GhostStatus.SOFT_WARNING
            pl["ghost_certainty"] = round(sr.certainty, 3)
            pl["ghost_warning"] = "soft_warning_validated_at_runtime"
            pl["ghost_reason"] = sr.reason
            pl["executable"] = True
            cs.action_payload = pl
            continue

        if sr.status == GhostStatus.MISSING:
            pl["ghost_status"] = GhostStatus.MISSING
            pl["ghost_certainty"] = round(sr.certainty, 3)
            pl["needs_user_label"] = True
            pl["ghost_missing"] = True
            pl["ghost_reason"] = sr.reason
            pl.setdefault(
                "label_prompt",
                f"Nevlan no encuentra {sr.human_label!r}. ¿Puedes señalarlo?",
            )
            cs.action_payload = pl
            n_missing += 1
            continue

        # OK / SKIPPED / UNCERTAIN: persistimos status para auditoría.
        pl["ghost_status"] = sr.status
        if sr.certainty:
            pl["ghost_certainty"] = round(sr.certainty, 3)
        cs.action_payload = pl
    return n_missing


__all__ = [
    "GhostStatus",
    "GhostStepReport",
    "GhostReport",
    "simulate_mission",
    "mark_missing_for_review",
    "semantic_certainty",
    "SEMANTIC_STRONG_THRESHOLD",
    "SEMANTIC_SOFT_THRESHOLD",
]
