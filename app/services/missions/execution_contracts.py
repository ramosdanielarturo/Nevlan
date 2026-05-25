"""
ArthurOS Services - Execution Contracts (Smart Executor)
--------------------------------------------------------
Define el contrato semántico de cada paso de una misión:

  - precondition  : ¿el sistema está en estado válido para ejecutar?
  - postcondition : ¿el sistema quedó en el estado deseado tras ejecutar?
  - preferred_strategy / fallback_strategies / recovery_strategy
  - checkpoint_key (opcional)

El Smart Executor consume estos contratos. Cada *kind* de paso tiene su
propio contrato; añadir un kind nuevo ⇒ una entrada nueva en
``_BUILDERS``.

Diseño:
  * Stateless / pure-data: las precondiciones son ``Callable[[StateSnapshot], bool]``.
  * Sin side-effects en este módulo: aquí solo se DESCRIBE qué validar y
    qué estrategias preferir. La ejecución real vive en ``action_runner.py``
    y la recuperación en ``recovery_engine.py``.
  * Centraliza el conocimiento de "cómo se ve un paso exitoso", para que
    el resto del sistema (logging, UX, learning) tenga una sola fuente
    de verdad.

Steps soportados (PRD inicial):
  - open_app          → abrir aplicación (Chrome, Excel...)
  - select_profile    → elegir perfil de Chrome/Edge tras lanzarlo
  - open_new_tab      → nueva pestaña en el navegador
  - open_url          → navegar a una URL conocida (con alias)
  - search_youtube    → [legacy] alias de búsqueda YouTube
  - search_content    → búsqueda en proveedor (UIC canónico)
  - search_site       → búsqueda dentro de un sitio nombrado

Cada contrato declara qué estrategias el ``ActionRunner`` debe intentar
y en qué orden. Strategy = string identificador; el matching está en
``action_runner.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.core.logger import log
from app.services.missions.launch_apps import (
    process_names_for,
    canonical_app_name,
)
from app.services.missions.state_detector import StateSnapshot


# ──────────────────────────────────────────────────────────────────────
# Step + Contract dataclasses
# ──────────────────────────────────────────────────────────────────────

@dataclass
class MissionStep:
    """Descripción semántica de un paso de la misión.

    Es la "intención" del usuario, no un evento crudo. El compiler legacy
    sigue produciendo ``CompiledStep`` (mission.py); ``MissionStep`` es la
    forma canónica del Smart Executor — más declarativa, sin coords.
    """

    id: str
    kind: str                           # "open_app" | "select_profile" | ...
    params: Dict[str, Any] = field(default_factory=dict)
    block_id: Optional[str] = None      # agrupación para checkpoints
    human_label: str = ""               # frase amigable: "Abrir Chrome"

    def describe(self) -> str:
        if self.human_label:
            return self.human_label
        if self.kind == "open_app":
            return f"Abrir {self.params.get('name', '?')}"
        if self.kind == "select_profile":
            return f"Seleccionar perfil {self.params.get('profile_name', '?')}"
        if self.kind == "use_selected_profile":
            return "Usar el perfil ya seleccionado"
        if self.kind == "open_new_tab":
            return "Abrir nueva pestaña"
        if self.kind == "open_url":
            url = self.params.get("url") or self.params.get("alias") or "?"
            return f"Abrir {url}"
        if self.kind == "search_youtube":
            q = self.params.get("query", "")
            return f"Buscar en YouTube: {q}"
        if self.kind == "search_content":
            q = self.params.get("query", "")
            prov = self.params.get("provider") or self.params.get("site") or ""
            tail = f" ({prov})" if prov else ""
            return f"Buscar contenido{tail}: {q}".strip()
        if self.kind == "search_site":
            q = self.params.get("query", "")
            site = self.params.get("site") or self.params.get("search_site") or ""
            tail = f" en {site}" if site else ""
            return f"Buscar{tail}: {q}".strip()
        if self.kind == "search_web":
            q = self.params.get("query", "")
            return f"Buscar en navegador: {q}"
        if self.kind == "open_search_result":
            t = self.params.get("title", "")
            return f"Abrir resultado: {t}"
        if self.kind == "scroll_page":
            return "Hacer scroll"
        return self.kind


# Predicates: callable(StateSnapshot) -> bool
Predicate = Callable[[StateSnapshot], bool]


@dataclass
class StepContract:
    """Contrato verificable de un paso.

    Las estrategias son **identificadores** (strings) que el ActionRunner
    sabe cómo ejecutar. Mantenerlas como strings permite que el
    ``LearningLogger`` ajuste prioridades sin acoplarse a callables.
    """

    step: MissionStep

    precondition: Predicate
    postcondition: Predicate

    # Orden de estrategias para ``ActionRunner``:
    preferred_strategy: str
    fallback_strategies: List[str] = field(default_factory=list)
    # Identificador de receta de recuperación en ``RecoveryEngine``.
    recovery_strategy: str = "generic"
    # Si el paso es un "hito" del bloque, ID del checkpoint a guardar.
    checkpoint_key: Optional[str] = None

    # Tiempos / presupuesto
    precondition_grace_ms: int = 0   # esperar antes de evaluar precondición
    postcondition_settle_ms: int = 800  # esperar antes de evaluar postcondición
    postcondition_timeout_ms: int = 6000  # reintento de postcondición

    # Mensajes humanos para UX (status normal):
    human_announce: str = ""         # "Verificando Chrome…"
    human_skip: str = ""             # "Chrome ya está abierto."
    human_done: str = ""             # "Chrome listo."
    human_recover: str = ""          # "No detecté YouTube, lo estoy corrigiendo."

    # PRD 2026-05-09 §exec-runtime: si el ``kind`` del step no tiene
    # builder registrado en ``_BUILDERS``, ``build_contract`` devuelve
    # un contrato sentinela con ``is_unknown_kind=True``. El
    # SmartExecutor lo detecta y falla rápido con
    # ``UNSUPPORTED_SEMANTIC_STEP_TYPE`` en lugar de entrar en bucle de
    # recovery infinito.
    is_unknown_kind: bool = False

    # Listado de todas las estrategias (preferred + fallbacks).
    def all_strategies(self) -> List[str]:
        out: List[str] = [self.preferred_strategy]
        for s in self.fallback_strategies:
            if s and s not in out:
                out.append(s)
        return out


# ──────────────────────────────────────────────────────────────────────
# Helpers de predicados
# ──────────────────────────────────────────────────────────────────────

def _always_true(_state: StateSnapshot) -> bool:
    return True


def _process_running(*procs: str) -> Predicate:
    def pred(s: StateSnapshot) -> bool:
        return s.is_process_running(*procs)
    return pred


def _domain_is(*domains: str) -> Predicate:
    def pred(s: StateSnapshot) -> bool:
        return s.domain_matches(*domains)
    return pred


def _url_contains(*frags: str) -> Predicate:
    def pred(s: StateSnapshot) -> bool:
        return s.url_contains(*frags)
    return pred


def _title_contains(*frags: str) -> Predicate:
    def pred(s: StateSnapshot) -> bool:
        return s.title_contains(*frags)
    return pred


def _and(*preds: Predicate) -> Predicate:
    def pred(s: StateSnapshot) -> bool:
        return all(p(s) for p in preds)
    return pred


def _or(*preds: Predicate) -> Predicate:
    def pred(s: StateSnapshot) -> bool:
        return any(p(s) for p in preds)
    return pred


def _not(pred: Predicate) -> Predicate:
    def inner(s: StateSnapshot) -> bool:
        return not pred(s)
    return inner


# ──────────────────────────────────────────────────────────────────────
# Builders por kind
# ──────────────────────────────────────────────────────────────────────
# Cada builder devuelve un ``StepContract`` listo. Mantengo aquí la
# regla del PRD literalmente — si el equipo cambia una postcondition,
# se cambia en UN sólo lugar y todo (executor, recovery, learning) la
# respeta.

def _build_open_app(step: MissionStep) -> StepContract:
    name = (step.params.get("name") or "").strip()
    procs = process_names_for(name) or (f"{name.lower()}.exe",)
    canonical = canonical_app_name(name) or name

    return StepContract(
        step=step,
        precondition=_always_true,
        postcondition=_process_running(*procs),
        preferred_strategy="open_app:os_startfile",
        fallback_strategies=[
            "open_app:start_command",
            "open_app:windows_search",
        ],
        recovery_strategy="recover_open_app",
        checkpoint_key=None,
        postcondition_settle_ms=1500,
        postcondition_timeout_ms=10000,
        human_announce=f"Verificando {canonical}…",
        human_skip=f"{canonical} ya está abierto.",
        human_done=f"{canonical} listo.",
        human_recover=f"No detecté {canonical}, lo estoy abriendo.",
    )


def _picker_visible_pred() -> Predicate:
    """¿Hay un profile picker de Chrome/Edge visible ahora mismo?"""
    return _title_contains(
        "Choose your profile",
        "Elige tu perfil",
        "Quién está usando",
        "Who's using Chrome",
        "¿Quién usa Chrome?",
        "Choose a profile",
    )


def _build_select_entity_from_collection(step: MissionStep) -> StepContract:
    """UCES — selección por entidad+colección (entity-first, sin coords primarias)."""
    pars = dict(step.params or {})
    ent_raw = pars.get("collection_entity") or pars.get("entity") or {}
    label = ""
    if isinstance(ent_raw, dict):
        label = str(ent_raw.get("display_name") or "").strip()
    label = label or str(pars.get("selection_label") or "").strip()
    picker_visible = _picker_visible_pred()
    pre = _or(
        picker_visible,
        _always_true,
    )
    post = _or(
        _not(picker_visible),
        _title_contains(label) if label else _always_true,
    )
    legacy = str(pars.get("fallback_legacy_type") or "select_profile")
    fallbacks = list(UCES_FALLBACKS_FOR_CONTRACT)
    if legacy == "select_profile":
        fallbacks = [
            "select_profile:uia_text_match",
            "select_profile:visible_text_ocr",
            *fallbacks,
        ]
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=post,
        preferred_strategy="select_entity:collection_first",
        fallback_strategies=fallbacks,
        recovery_strategy="recover_select_profile",
        checkpoint_key="entity_in_collection_selected",
        postcondition_settle_ms=1200,
        postcondition_timeout_ms=8000,
        human_announce=(
            f"Seleccionando {label}…" if label else "Seleccionando elemento…"
        ),
        human_skip="El elemento ya está seleccionado.",
        human_done="Elemento listo.",
        human_recover="No encontré el elemento, reintentando.",
    )


UCES_FALLBACKS_FOR_CONTRACT: Tuple[str, ...] = (
    "select_entity:operational_entity_match",
    "select_entity:legacy_kind_fallback",
)


def _build_select_profile(step: MissionStep) -> StepContract:
    profile = (step.params.get("profile_name") or "").strip()
    picker_visible = _picker_visible_pred()
    # Pre: o estamos en el profile picker, o Chrome aún no cargó del
    # todo (ventana sin URL real).
    pre_picker_visible = _or(
        picker_visible,
        # Chrome lanzado pero no terminó de cargar:
        _and(
            _process_running("chrome.exe"),
            _not(_or(_url_contains("http://"), _url_contains("https://"))),
        ),
    )
    # Post (PRD 2026-05-03d §6): profile picker cerrado + Chrome activo +
    # (idealmente) título coincide con el profile_name. Si no hay perfil
    # configurado, basta con que Chrome ya cargó una URL real.
    chrome_ready = _and(
        _process_running("chrome.exe"),
        _or(_url_contains("http://"), _url_contains("https://")),
    )
    post = _and(
        _not(picker_visible),  # picker ya NO está visible
        _or(
            chrome_ready,
            _title_contains(profile) if profile else chrome_ready,
        ),
    )
    return StepContract(
        step=step,
        precondition=pre_picker_visible,
        postcondition=post,
        preferred_strategy="select_profile:skip_if_loaded",
        fallback_strategies=[
            "select_profile:ocr_collection",
            "select_profile:uia_text_match",
            "select_profile:visible_text_ocr",
            "select_profile:visual_asset",
            "select_profile:relative_coords",
        ],
        recovery_strategy="recover_select_profile",
        checkpoint_key="chrome_profile_loaded",
        postcondition_settle_ms=1200,
        postcondition_timeout_ms=8000,
        human_announce=(
            f"Seleccionando perfil {profile}…" if profile
            else "Seleccionando perfil…"
        ),
        human_skip="El perfil ya está cargado.",
        human_done="Perfil listo.",
        human_recover="No vi el perfil, vuelvo a intentar.",
    )


def _build_open_new_tab(step: MissionStep) -> StepContract:
    pre = _process_running("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe")
    # Post: hay una pestaña con about:blank, chrome://newtab, o cualquier
    # URL nueva. Como heurística práctica, validamos que el navegador
    # sigue activo + ha cambiado el título O la URL es una "nueva
    # pestaña" típica.
    post_new_tab_signals = _or(
        _url_contains("about:blank", "newtab", "chrome://newtab"),
        # También vale "no hay URL todavía, pero el browser está activo"
        # — la postcondition real se valida en composición con el
        # siguiente step (open_url).
        _and(pre, _title_contains("Nueva pestaña", "New Tab")),
    )
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=post_new_tab_signals,
        preferred_strategy="open_new_tab:hotkey_ctrl_t",
        fallback_strategies=[
            "open_new_tab:uia_button",
            "open_new_tab:vision_coords",
        ],
        recovery_strategy="recover_open_new_tab",
        checkpoint_key=None,
        postcondition_settle_ms=500,
        postcondition_timeout_ms=4000,
        human_announce="Abriendo pestaña nueva…",
        human_done="Pestaña lista.",
        human_recover="La pestaña no abrió, reintentando.",
    )


def resolve_url_alias(step: MissionStep) -> Tuple[str, str]:
    """Devuelve ``(url, expected_domain)`` desde params.

    Acepta ``url`` directo o ``alias`` (``youtube`` → ``youtube.com``).
    Pública: la usa también ``action_runner.py``.
    """
    direct = (step.params.get("url") or "").strip()
    alias = (
        step.params.get("alias")
        or step.params.get("site")
        or ""
    ).strip().lower()
    aliases = {
        "youtube": ("https://www.youtube.com", "youtube.com"),
        "google": ("https://www.google.com", "google.com"),
        "gmail": ("https://mail.google.com", "mail.google.com"),
        "drive": ("https://drive.google.com", "drive.google.com"),
        "github": ("https://github.com", "github.com"),
    }
    if direct:
        try:
            host = urlparse(direct).netloc or direct
        except Exception:
            host = direct
        return direct, host.lower().lstrip("www.")
    if alias in aliases:
        return aliases[alias]
    if alias:
        return f"https://{alias}", alias
    return "", ""


def _is_youtube_search_target(params: Optional[Dict[str, Any]]) -> bool:
    """Destino YouTube explícito vía ``provider`` o ``site``."""
    if not params:
        return False
    prov = str(params.get("provider") or "").strip().lower()
    site = str(params.get("site") or params.get("search_site") or "").strip().lower()
    return prov == "youtube" or site == "youtube"


def _youtube_search_step_contract(
    step: MissionStep,
    *,
    default_preferred: str,
    default_fallbacks: List[str],
) -> StepContract:
    """Pre/post condiciones YouTube DOM para ``search_*`` canónicos."""
    params = step.params or {}
    query = (params.get("query") or "").strip()
    pref = str(params.get("preferred_strategy") or default_preferred).strip()
    fb_raw = params.get("fallback_strategies")
    if isinstance(fb_raw, list) and fb_raw:
        fallbacks = [str(x) for x in fb_raw if str(x).strip()]
    else:
        fallbacks = list(default_fallbacks)

    pre = _or(
        _domain_is("youtube.com"),
        _url_contains("youtube.com"),
        _title_contains("youtube"),
    )

    post_signals: List[Predicate] = [
        _url_contains("/results?"),
        _url_contains("search_query="),
    ]
    if query:
        first_token = query.split()[0] if query else ""
        if first_token and len(first_token) >= 3:
            post_signals.append(_title_contains(first_token))
    post = _or(*post_signals)

    return StepContract(
        step=step,
        precondition=pre,
        postcondition=post,
        preferred_strategy=pref,
        fallback_strategies=fallbacks,
        recovery_strategy="recover_search",
        checkpoint_key="search_results_visible",
        postcondition_settle_ms=1200,
        postcondition_timeout_ms=8000,
        human_announce=(
            f"Buscando en YouTube: {query}…" if query else "Buscando en YouTube…"
        ),
        human_done="Resultados listos.",
        human_recover="No encontré la barra de búsqueda, reintentando.",
    )


def _build_open_url(step: MissionStep) -> StepContract:
    url, expected_domain = resolve_url_alias(step)
    pre = _process_running("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe")

    # PRD 2026-05-03d §7: la postcondición acepta cualquier señal
    # fuerte de que el sitio está cargado:
    #   * domain_matches (Playwright/CDP → browser_domain)
    #   * url_contains   (Playwright/CDP → browser_url)
    #   * title_contains (Win32 → sin CDP también funciona)
    short = expected_domain.split(".")[0] if expected_domain else ""
    post_signals: List[Predicate] = []
    if str(url or "").lower().startswith("file:"):
        from pathlib import Path as _Path
        from urllib.parse import unquote, urlparse as _urlparse

        raw_path = unquote(_urlparse(url).path)
        if raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        fname = _Path(raw_path).name
        stem = _Path(raw_path).stem
        if fname:
            post_signals.append(_url_contains(fname))
        if stem:
            post_signals.append(_title_contains(stem))
    elif expected_domain:
        post_signals.append(_domain_is(expected_domain))
        post_signals.append(_url_contains(expected_domain))
    if short:
        # p.ej. "youtube", "google", "github".
        post_signals.append(_title_contains(short))
    if not post_signals:
        post_signals.append(_url_contains("http"))
    post = _or(*post_signals)

    # Checkpoint redundante: alias amigable además del dominio exacto.
    # El RecoveryEngine busca 'youtube_loaded' **y** 'youtube.com_loaded',
    # así que dejamos el segundo y también un hook para el alias.
    checkpoint_key = f"{expected_domain}_loaded" if expected_domain else None

    label = expected_domain or "la URL"
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=post,
        preferred_strategy="open_url:hotkey_ctrl_l",
        fallback_strategies=[
            "open_url:playwright_goto",
            "open_url:bookmark_uia",
            "open_url:vision_coords",
        ],
        recovery_strategy="recover_open_url",
        checkpoint_key=checkpoint_key,
        postcondition_settle_ms=1500,
        postcondition_timeout_ms=12000,
        human_announce=f"Abriendo {label}…",
        human_done=f"{label} cargado.",
        human_recover=f"No detecté {label}, lo estoy corrigiendo.",
    )


def _build_open_site(step: MissionStep) -> StepContract:
    """UIC ``open_site``: mismo contrato que ``open_url`` con smart_route primario."""
    params = step.params or {}
    base = _build_open_url(step)
    pref = str(params.get("preferred_strategy") or "open_site:smart_route").strip()
    fb_raw = params.get("fallback_strategies")
    if isinstance(fb_raw, list) and fb_raw:
        fallbacks = [str(x) for x in fb_raw if str(x).strip()]
    else:
        fallbacks = [
            "open_url:hotkey_ctrl_l",
            "open_url:playwright_goto",
            "open_url:bookmark_uia",
            "open_url:vision_coords",
        ]
    return StepContract(
        step=step,
        precondition=base.precondition,
        postcondition=base.postcondition,
        preferred_strategy=pref,
        fallback_strategies=fallbacks,
        recovery_strategy=base.recovery_strategy,
        checkpoint_key=base.checkpoint_key,
        postcondition_settle_ms=base.postcondition_settle_ms,
        postcondition_timeout_ms=base.postcondition_timeout_ms,
        human_announce=base.human_announce,
        human_done=base.human_done,
        human_recover=base.human_recover,
    )


def _build_search_youtube(step: MissionStep) -> StepContract:
    """Contrato legacy — mimético al histórico ``dom_input`` primero."""
    return _youtube_search_step_contract(
        step,
        default_preferred="search_youtube:dom_input",
        default_fallbacks=[
            "search_youtube:dom_search_input",
            "search_youtube:uia_searchbox",
            "search_youtube:ocr_buscar",
            "search_youtube:visual_asset",
            "search_youtube:relative_coords",
        ],
    )


def _build_search_content(step: MissionStep) -> StepContract:
    """UIC ``search_content``: smart_route primero; YouTube vía rutas genéricas."""
    params = step.params or {}
    if _is_youtube_search_target(params):
        return _youtube_search_step_contract(
            step,
            default_preferred="search_content:smart_route",
            default_fallbacks=[
                "search_site:smart_route",
                "search_site:youtube_dom_input",
                "search_youtube:dom_input",
                "search_youtube:dom_search_input",
                "search_youtube:uia_searchbox",
                "search_youtube:ocr_buscar",
                "search_youtube:keyboard_focus_then_type",
            ],
        )
    sw_params = dict(params)
    if not sw_params.get("engine") and sw_params.get("provider"):
        sw_params["engine"] = str(sw_params.get("provider")).strip().lower()
    sw_step = MissionStep(
        id=step.id,
        kind="search_web",
        params=sw_params,
        block_id=step.block_id,
        human_label=step.human_label,
    )
    base = _build_search_web(sw_step)
    pref = str(params.get("preferred_strategy") or "search_content:smart_route").strip()
    fb_raw = params.get("fallback_strategies")
    if isinstance(fb_raw, list) and fb_raw:
        fallbacks = [str(x) for x in fb_raw if str(x).strip()]
    else:
        fallbacks = [
            "search_site:smart_route",
            "search_web:address_bar_type_enter",
            "search_web:dom_search_input",
            "search_web:keyboard_focus_then_type",
        ]
    return StepContract(
        step=step,
        precondition=base.precondition,
        postcondition=base.postcondition,
        preferred_strategy=pref,
        fallback_strategies=fallbacks,
        recovery_strategy=base.recovery_strategy,
        checkpoint_key=base.checkpoint_key,
        postcondition_settle_ms=base.postcondition_settle_ms,
        postcondition_timeout_ms=base.postcondition_timeout_ms,
        human_announce=base.human_announce,
        human_done=base.human_done,
        human_recover=base.human_recover,
    )


def _build_search_site(step: MissionStep) -> StepContract:
    """UIC ``search_site``: smart_route + rutas específicas sólo como fallback."""
    params = step.params or {}
    if _is_youtube_search_target(params):
        return _youtube_search_step_contract(
            step,
            default_preferred="search_site:smart_route",
            default_fallbacks=[
                "search_site:youtube_dom_input",
                "search_youtube:dom_input",
                "search_web:address_bar_type_enter",
                "search_web:dom_search_input",
                "search_web:keyboard_focus_then_type",
            ],
        )
    sw_params = dict(params)
    if not sw_params.get("engine"):
        site_hint = str(sw_params.get("site") or sw_params.get("search_site") or "").strip()
        if site_hint:
            sw_params["engine"] = site_hint.lower()
    sw_step = MissionStep(
        id=step.id,
        kind="search_web",
        params=sw_params,
        block_id=step.block_id,
        human_label=step.human_label,
    )
    base = _build_search_web(sw_step)
    pref = str(params.get("preferred_strategy") or "search_site:smart_route").strip()
    fb_raw = params.get("fallback_strategies")
    if isinstance(fb_raw, list) and fb_raw:
        fallbacks = [str(x) for x in fb_raw if str(x).strip()]
    else:
        fallbacks = [
            "search_web:address_bar_type_enter",
            "search_web:dom_search_input",
            "search_web:keyboard_focus_then_type",
        ]
    return StepContract(
        step=step,
        precondition=base.precondition,
        postcondition=base.postcondition,
        preferred_strategy=pref,
        fallback_strategies=fallbacks,
        recovery_strategy=base.recovery_strategy,
        checkpoint_key=base.checkpoint_key,
        postcondition_settle_ms=base.postcondition_settle_ms,
        postcondition_timeout_ms=base.postcondition_timeout_ms,
        human_announce=base.human_announce,
        human_done=base.human_done,
        human_recover=base.human_recover,
    )


def _build_scroll_results(step: MissionStep) -> StepContract:
    """Scroll dentro de una página de resultados (típicamente YouTube).

    No hay un postcondition fuerte verificable sin OCR/Vision (el
    scroll es por definición "cambiar de viewport"). Aceptamos que se
    cumple si la URL sigue siendo de resultados y el navegador está
    activo — es un step "siempre éxito" salvo excepción del runner.
    """
    context = (step.params.get("context") or "youtube_results").strip().lower()
    pre = _process_running(
        "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe",
    )
    if "youtube" in context:
        post: Predicate = _or(
            _domain_is("youtube.com"),
            _url_contains("youtube.com"),
            _title_contains("youtube"),
        )
    else:
        post = pre
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=post,
        preferred_strategy="scroll_results:wheel",
        fallback_strategies=[
            "scroll_results:keyboard",
            "scroll_results:js_scrollby",
        ],
        recovery_strategy="generic",
        checkpoint_key=None,
        postcondition_settle_ms=400,
        postcondition_timeout_ms=2000,
        human_announce="Bajando para ver más resultados…",
        human_done="Listo.",
        human_recover="No pude hacer scroll, reintentando.",
    )


def _build_use_selected_profile(step: MissionStep) -> StepContract:
    """``use_selected_profile`` — Chrome ya está activo y queremos
    seguir usando el perfil que ya está cargado (sin tocar el profile
    picker).

    Pre: cualquier navegador soportado está en ejecución (chrome /
    edge / brave / firefox).
    Post: idem — basta con que el navegador siga vivo. Es un paso
    semánticamente "no-op seguro" cuando el usuario explicitamente
    indicó "usa el perfil que ya está".
    """
    pre = _process_running(
        "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe",
    )
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=pre,
        preferred_strategy="use_selected_profile:noop",
        fallback_strategies=[
            "use_selected_profile:check_chrome_active",
        ],
        recovery_strategy="generic",
        checkpoint_key=None,
        postcondition_settle_ms=200,
        postcondition_timeout_ms=2000,
        human_announce="Usando el perfil ya seleccionado…",
        human_done="Perfil activo confirmado.",
        human_recover="No detecté Chrome activo, reintentando.",
    )


def _build_search_web(step: MissionStep) -> StepContract:
    """``search_web`` — buscar una query desde la barra de direcciones
    del navegador (Ctrl+L → tipear → Enter).

    Pre: navegador activo.
    Post: la URL/título reflejan que la búsqueda fue ejecutada (URL
    contiene engine de búsqueda o el título contiene un fragmento de
    la query).
    """
    query = (step.params.get("query") or "").strip()
    engine = (step.params.get("engine") or "google").strip().lower()
    pre = _process_running(
        "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe",
    )
    post_signals: List[Predicate] = [
        _url_contains("search?", "/search?"),
        _url_contains("q="),
        _url_contains("/results?"),
    ]
    if engine in ("google", "duckduckgo", "bing"):
        post_signals.append(_url_contains(f"{engine}.com"))
    if query:
        first_token = query.split()[0]
        if first_token and len(first_token) >= 3:
            post_signals.append(_title_contains(first_token))
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=_or(*post_signals),
        preferred_strategy="search_web:address_bar_type_enter",
        fallback_strategies=[
            "search_web:dom_search_input",
            "search_web:keyboard_focus_then_type",
        ],
        recovery_strategy="generic",
        checkpoint_key="search_web_results_visible",
        postcondition_settle_ms=1000,
        postcondition_timeout_ms=8000,
        human_announce=(
            f"Buscando en {engine}: {query}…" if query
            else f"Buscando en {engine}…"
        ),
        human_done="Resultados de búsqueda listos.",
        human_recover="No vi la barra de búsqueda, reintentando.",
    )


def _build_open_search_result(step: MissionStep) -> StepContract:
    """``open_search_result`` — click en un resultado de búsqueda
    identificado por ``params["title"]``.

    Pre: navegador activo.
    Post: el título de la pestaña contiene un fragmento del resultado
    abierto (señal blanda de que la navegación ocurrió).
    """
    title = (step.params.get("title") or "").strip()
    pre = _process_running(
        "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe",
    )
    post_signals: List[Predicate] = []
    if title:
        # Tomamos el primer token "fuerte" del título para validar
        # navegación (p.ej. "Banxico" en
        # "Banxico, banco central, Banco de México").
        for tok in title.replace(",", " ").split():
            tok = tok.strip()
            if len(tok) >= 4:
                post_signals.append(_title_contains(tok))
                post_signals.append(_url_contains(tok.lower()))
                break
    if not post_signals:
        post_signals.append(_url_contains("http"))
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=_or(*post_signals),
        preferred_strategy="open_search_result:dom_link_by_title",
        fallback_strategies=[
            "open_search_result:uia_text_match",
            "open_search_result:keyboard_tab_then_enter",
        ],
        recovery_strategy="generic",
        checkpoint_key=None,
        postcondition_settle_ms=1000,
        postcondition_timeout_ms=10000,
        human_announce=(
            f"Abriendo resultado: {title}…" if title
            else "Abriendo resultado…"
        ),
        human_done="Resultado abierto.",
        human_recover="No encontré el resultado, reintentando.",
    )


def _build_scroll_page(step: MissionStep) -> StepContract:
    """``scroll_page`` — scroll genérico sobre la página actual del
    navegador (no asume YouTube).

    Pre: navegador activo. Post: idem (el scroll no cambia URL/título;
    es fire-and-forget). Si pyautogui está ausente, falla en runtime
    pero al menos el contract no impide que el executor avance.
    """
    pre = _process_running(
        "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe",
    )
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=pre,
        preferred_strategy="scroll_page:wheel",
        fallback_strategies=[
            "scroll_page:keyboard",
            "scroll_page:js_scrollby",
        ],
        recovery_strategy="generic",
        checkpoint_key=None,
        postcondition_settle_ms=300,
        postcondition_timeout_ms=2000,
        human_announce="Bajando para ver más…",
        human_done="Listo.",
        human_recover="No pude hacer scroll, reintentando.",
    )


def _build_fill_form(step: MissionStep) -> StepContract:
    """UIC ``fill_form`` — llenar campo(s) vía UOL (editor o formulario)."""
    params = step.params or {}
    pref = str(params.get("preferred_strategy") or "fill_form:uol_fields").strip()
    fb_raw = params.get("fallback_strategies")
    if isinstance(fb_raw, list) and fb_raw:
        fallbacks = [str(x) for x in fb_raw if str(x).strip()]
    else:
        fallbacks = [
            "fill_field:uol_field",
            "search_web:keyboard_focus_then_type",
        ]
    text = str(
        params.get("text")
        or params.get("value")
        or params.get("content")
        or params.get("multiline_text")
        or "",
    ).strip()
    field = str(params.get("field") or params.get("target") or "").strip()

    def _post(state: StateSnapshot) -> bool:
        probe = text or field
        if not probe:
            return False
        snippet = probe[: min(16, len(probe))]
        if len(snippet) >= 3 and state.title_contains(snippet):
            return True
        visible = (state.visible_text or "").lower()
        if visible and probe.lower() in visible:
            return True
        return False

    pre = _or(
        _process_running(
            "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe",
            "notepad.exe", "wordpad.exe",
        ),
        _always_true,
    )
    label = field or "campo"
    return StepContract(
        step=step,
        precondition=pre,
        postcondition=_post,
        preferred_strategy=pref,
        fallback_strategies=fallbacks,
        recovery_strategy="generic",
        checkpoint_key=None,
        postcondition_settle_ms=400,
        postcondition_timeout_ms=4000,
        human_announce=f"Rellenando {label}…" if label else "Rellenando formulario…",
        human_done="Campo listo.",
        human_recover="No pude rellenar el campo, reintentando.",
    )


def _build_submit_form(step: MissionStep) -> StepContract:
    """UIC ``submit_form`` — enviar formulario o persistir (save) vía UOL."""
    params = step.params or {}
    pref = str(params.get("preferred_strategy") or "submit_form:uol_submit").strip()
    fb_raw = params.get("fallback_strategies")
    if isinstance(fb_raw, list) and fb_raw:
        fallbacks = [str(x) for x in fb_raw if str(x).strip()]
    else:
        fallbacks = [
            "submit_input:uol_submit",
            "confirm_dialog:dismiss_positive",
        ]
    path = str(
        params.get("path")
        or params.get("filename")
        or params.get("file")
        or "",
    ).strip()

    def _post(state: StateSnapshot) -> bool:
        if not path:
            return False
        stem = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        base = stem.rsplit(".", 1)[0] if "." in stem else stem
        if base and len(base) >= 2 and state.title_contains(base):
            return True
        return False

    return StepContract(
        step=step,
        precondition=_always_true,
        postcondition=_post,
        preferred_strategy=pref,
        fallback_strategies=fallbacks,
        recovery_strategy="generic",
        checkpoint_key=None,
        postcondition_settle_ms=500,
        postcondition_timeout_ms=5000,
        human_announce="Enviando formulario…",
        human_done="Formulario enviado.",
        human_recover="No pude enviar el formulario, reintentando.",
    )


def _build_switch_context(step: MissionStep) -> StepContract:
    """UIC ``switch_context`` — foco de ventana o transición de contexto vía UOL."""
    params = step.params or {}
    pref = str(params.get("preferred_strategy") or "switch_context:uol_context").strip()
    fb_raw = params.get("fallback_strategies")
    if isinstance(fb_raw, list) and fb_raw:
        fallbacks = [str(x) for x in fb_raw if str(x).strip()]
    else:
        fallbacks = ["wait_for_state:poll"]
    action = str(params.get("action") or params.get("method") or "").strip().lower()
    target = str(
        params.get("target")
        or params.get("window")
        or params.get("title")
        or "",
    ).strip()

    def _post(state: StateSnapshot) -> bool:
        if action in ("close_unsaved", "close"):
            vis = (state.visible_text or "").lower()
            markers = (
                "save",
                "don't save",
                "unsaved",
                "guardar",
                "descartar",
                "cambios",
            )
            if any(m in vis for m in markers):
                return True
            return False
        if target and state.title_contains(target):
            return True
        return False

    return StepContract(
        step=step,
        precondition=_always_true,
        postcondition=_post,
        preferred_strategy=pref,
        fallback_strategies=fallbacks,
        recovery_strategy="generic",
        checkpoint_key=None,
        postcondition_settle_ms=400,
        postcondition_timeout_ms=4000,
        human_announce="Cambiando contexto…",
        human_done="Contexto listo.",
        human_recover="No pude cambiar el contexto, reintentando.",
    )


def _build_confirm_dialog(step: MissionStep) -> StepContract:
    """UIC ``confirm_dialog`` — elegir opción en diálogo modal vía UOL."""
    params = step.params or {}
    pref = str(params.get("preferred_strategy") or "confirm_dialog:uol_dialog").strip()
    fb_raw = params.get("fallback_strategies")
    if isinstance(fb_raw, list) and fb_raw:
        fallbacks = [str(x) for x in fb_raw if str(x).strip()]
    else:
        fallbacks = [
            "submit_input:uol_submit",
            "confirm:ui",
        ]
    choice = str(
        params.get("choice")
        or params.get("answer")
        or params.get("action")
        or "",
    ).strip().lower()

    def _post(state: StateSnapshot) -> bool:
        vis = (state.visible_text or "").lower()
        dialog_markers = (
            "do you want to save",
            "don't save",
            "no guardar",
            "guardar",
            "save changes",
            "descartar",
        )
        if any(m in vis for m in dialog_markers):
            return False
        return False

    return StepContract(
        step=step,
        precondition=_always_true,
        postcondition=_post,
        preferred_strategy=pref,
        fallback_strategies=fallbacks,
        recovery_strategy="generic",
        checkpoint_key=None,
        postcondition_settle_ms=400,
        postcondition_timeout_ms=4000,
        human_announce="Confirmando diálogo…",
        human_done="Diálogo cerrado.",
        human_recover="No pude confirmar el diálogo, reintentando.",
    )


# Mapa kind → builder
_BUILDERS: Dict[str, Callable[[MissionStep], StepContract]] = {
    "open_app": _build_open_app,
    "select_entity_from_collection": _build_select_entity_from_collection,
    "select_profile": _build_select_profile,
    "use_selected_profile": _build_use_selected_profile,
    "open_new_tab": _build_open_new_tab,
    "open_url": _build_open_url,
    "open_site": _build_open_site,
    "search_content": _build_search_content,
    "search_site": _build_search_site,
    "search_youtube": _build_search_youtube,
    "scroll_results": _build_scroll_results,
    "search_web": _build_search_web,
    "open_search_result": _build_open_search_result,
    "scroll_page": _build_scroll_page,
    "fill_form": _build_fill_form,
    "submit_form": _build_submit_form,
    "switch_context": _build_switch_context,
    "confirm_dialog": _build_confirm_dialog,
}


def register_step_kind(
    kind: str,
    builder: Callable[[MissionStep], StepContract],
) -> None:
    """Permite a otros módulos registrar nuevos contratos sin tocar este file."""
    _BUILDERS[kind] = builder


def build_contract(step: MissionStep) -> StepContract:
    """Construye el ``StepContract`` para ``step``.

    Si el ``kind`` no está registrado, devuelve un contrato sentinela
    con ``is_unknown_kind=True``. El SmartExecutor lo detecta y falla
    rápido con ``UNSUPPORTED_SEMANTIC_STEP_TYPE`` en lugar de quedarse
    en bucle de recovery infinito (PRD 2026-05-09 §exec-runtime).
    """
    builder = _BUILDERS.get(step.kind)
    if not builder:
        log.warning(f"build_contract: kind desconocido '{step.kind}'")
        return StepContract(
            step=step,
            precondition=_always_true,
            # Postcondition imposible: garantiza que ningún code-path
            # accidental marque success silencioso si el caller ignora
            # ``is_unknown_kind``.
            postcondition=lambda s: False,
            preferred_strategy="unknown",
            fallback_strategies=[],
            recovery_strategy="generic",
            human_announce=step.describe(),
            human_recover="Paso desconocido, pido intervención.",
            is_unknown_kind=True,
        )
    return builder(step)


def supported_kinds() -> List[str]:
    """Devuelve el listado ordenado de ``kind`` con builder registrado.

    Útil para construir el mensaje del error
    ``UNSUPPORTED_SEMANTIC_STEP_TYPE``: cuando un plan trae un tipo
    sin builder, le decimos al usuario qué tipos SÍ están soportados
    para que pueda corregir el plan o reportar el bug.
    """
    return sorted(_BUILDERS.keys())


__all__ = [
    "MissionStep",
    "StepContract",
    "Predicate",
    "build_contract",
    "register_step_kind",
    "resolve_url_alias",
    "supported_kinds",
]
