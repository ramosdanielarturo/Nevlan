"""
Nevlan — UIC Capability Registry (contrato cerrado para SEP).

Define capacidades canónicas con familia, estrategias preferidas,
validadores de outcome y políticas de seguridad. El SEP se valida
contra este registro; tipos desconocidos ⇒ revisión humana explícita.

No todas las capacidades tienen runtime profundo todavía; el registry
es la fuente de verdad para producto + tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

# ── Familias mínimas (16) ────────────────────────────────────────────

UIC_FAMILY_SYSTEM_NAVIGATION = "system_navigation"
UIC_FAMILY_BROWSER = "browser"
UIC_FAMILY_FIELDS_TEXT = "fields_text"
UIC_FAMILY_CLIPBOARD_DATA = "clipboard_data"
UIC_FAMILY_VISUAL_INTERACTION = "visual_interaction"
UIC_FAMILY_BUTTONS_MENUS_OPTIONS = "buttons_menus_options"
UIC_FAMILY_FILES = "files"
UIC_FAMILY_TABLES_DATA = "tables_data"
UIC_FAMILY_AUTHENTICATION = "authentication"
UIC_FAMILY_VALIDATION_OUTCOME = "validation_outcome"
UIC_FAMILY_SELF_HEALING = "self_healing"
UIC_FAMILY_WORKFLOW_SEMANTICS = "workflow_semantics"
UIC_FAMILY_TEMPORAL_WAITS = "temporal_waits"
UIC_FAMILY_HOTKEY_ENGINE = "hotkey_engine"
UIC_FAMILY_CONTEXT_MEMORY = "context_memory"
UIC_FAMILY_RERECORD_REPAIR = "rerecord_repair"

UIC_FAMILIES: Tuple[str, ...] = (
    UIC_FAMILY_SYSTEM_NAVIGATION,
    UIC_FAMILY_BROWSER,
    UIC_FAMILY_FIELDS_TEXT,
    UIC_FAMILY_CLIPBOARD_DATA,
    UIC_FAMILY_VISUAL_INTERACTION,
    UIC_FAMILY_BUTTONS_MENUS_OPTIONS,
    UIC_FAMILY_FILES,
    UIC_FAMILY_TABLES_DATA,
    UIC_FAMILY_AUTHENTICATION,
    UIC_FAMILY_VALIDATION_OUTCOME,
    UIC_FAMILY_SELF_HEALING,
    UIC_FAMILY_WORKFLOW_SEMANTICS,
    UIC_FAMILY_TEMPORAL_WAITS,
    UIC_FAMILY_HOTKEY_ENGINE,
    UIC_FAMILY_CONTEXT_MEMORY,
    UIC_FAMILY_RERECORD_REPAIR,
)

OutcomeValidator = Callable[[Dict[str, Any]], Optional[str]]
SafetyPolicy = str  # token estable para UI / auditoría


def _passthrough_validator(_step: Dict[str, Any]) -> Optional[str]:
    return None


def _need_query(step: Dict[str, Any]) -> Optional[str]:
    if step.get("needs_user_label"):
        return None
    p = step.get("params") or {}
    q = str(p.get("query") or "").strip()
    return None if q else "MISSING_QUERY"


def _need_provider_or_site(step: Dict[str, Any]) -> Optional[str]:
    if step.get("needs_user_label"):
        return None
    p = step.get("params") or {}
    prov = str(p.get("provider") or "").strip().lower()
    site = str(p.get("site") or p.get("search_site") or "").strip().lower()
    return None if prov or site else "MISSING_PROVIDER_OR_SITE"


def _need_profile(step: Dict[str, Any]) -> Optional[str]:
    p = step.get("params") or {}
    name = str(p.get("profile_name") or p.get("profile") or "").strip()
    if step.get("needs_user_label"):
        return None
    return None if name else "MISSING_PROFILE_NAME"


@dataclass(frozen=True)
class UICCapability:
    capability_id: str
    family: str
    description: str
    semantic_type: str
    preferred_strategies: Tuple[str, ...]
    fallback_strategies: Tuple[str, ...] = ()
    required_params: Tuple[str, ...] = ()
    optional_params: Tuple[str, ...] = ()
    outcome_validator: OutcomeValidator = field(default=_passthrough_validator, repr=False)
    safety_policy: SafetyPolicy = "default"
    allows_coords_emergency: bool = False
    hotkey_first: bool = False
    supports_rerecord: bool = True
    supports_self_heal: bool = True
    legacy_alias: bool = False


_CAPABILITIES: Dict[str, UICCapability] = {
    "open_app": UICCapability(
        capability_id="open_app",
        family=UIC_FAMILY_SYSTEM_NAVIGATION,
        description="Abrir una aplicación del sistema",
        semantic_type="launch",
        preferred_strategies=("open_app:windows_search",),
        fallback_strategies=("open_app:os_startfile", "open_app:start_command"),
        required_params=("app",),
        optional_params=("method", "name"),
        outcome_validator=_passthrough_validator,
        hotkey_first=False,
    ),
    "open_url": UICCapability(
        capability_id="open_url",
        family=UIC_FAMILY_BROWSER,
        description="Navegar a URL o alias conocido",
        semantic_type="navigate",
        preferred_strategies=("open_url:hotkey_ctrl_l",),
        fallback_strategies=(
            "open_url:playwright_goto",
            "open_url:bookmark_uia",
        ),
        required_params=(),
        optional_params=("url", "alias", "name"),
        outcome_validator=_passthrough_validator,
        hotkey_first=True,
    ),
    "open_new_tab": UICCapability(
        capability_id="open_new_tab",
        family=UIC_FAMILY_BROWSER,
        description="Nueva pestaña en el navegador",
        semantic_type="tab",
        preferred_strategies=("open_new_tab:hotkey_ctrl_t",),
        fallback_strategies=("open_new_tab:uia_button",),
        optional_params=("app",),
        outcome_validator=_passthrough_validator,
        hotkey_first=True,
    ),
    "focus_browser_address_bar": UICCapability(
        capability_id="focus_browser_address_bar",
        family=UIC_FAMILY_BROWSER,
        description="Enfocar la barra de direcciones",
        semantic_type="focus",
        preferred_strategies=("open_url:hotkey_ctrl_l",),
        outcome_validator=_passthrough_validator,
        hotkey_first=True,
    ),
    "focus_search_field": UICCapability(
        capability_id="focus_search_field",
        family=UIC_FAMILY_FIELDS_TEXT,
        description="Enfocar campo de búsqueda contextual",
        semantic_type="focus",
        preferred_strategies=("search_web:keyboard_focus_then_type",),
        outcome_validator=_passthrough_validator,
    ),
    "type_field_value": UICCapability(
        capability_id="type_field_value",
        family=UIC_FAMILY_FIELDS_TEXT,
        description="Escribir texto en campo enfocado",
        semantic_type="input",
        preferred_strategies=("type_field:keyboard",),
        outcome_validator=_passthrough_validator,
    ),
    "submit_field": UICCapability(
        capability_id="submit_field",
        family=UIC_FAMILY_FIELDS_TEXT,
        description="Confirmar campo (Enter/submit)",
        semantic_type="submit",
        preferred_strategies=("submit_field:enter",),
        outcome_validator=_passthrough_validator,
        hotkey_first=True,
    ),
    "search_content": UICCapability(
        capability_id="search_content",
        family=UIC_FAMILY_BROWSER,
        description="Buscar contenido en un proveedor (ej. youtube)",
        semantic_type="search",
        preferred_strategies=("search_content:smart_route",),
        fallback_strategies=(
            "search_site:smart_route",
            "search_site:youtube_dom_input",
            "search_youtube:dom_input",
        ),
        required_params=("query",),
        optional_params=("provider", "site", "target"),
        outcome_validator=_need_provider_or_site,
    ),
    "search_site": UICCapability(
        capability_id="search_site",
        family=UIC_FAMILY_BROWSER,
        description="Buscar dentro de un sitio nombrado",
        semantic_type="search",
        preferred_strategies=("search_site:smart_route",),
        fallback_strategies=(
            "search_site:youtube_dom_input",
            "search_web:address_bar_type_enter",
        ),
        required_params=("query",),
        optional_params=("site", "search_site", "target"),
        outcome_validator=_need_query,
    ),
    "search_web": UICCapability(
        capability_id="search_web",
        family=UIC_FAMILY_BROWSER,
        description="Buscar en motor web (barra del navegador)",
        semantic_type="search",
        preferred_strategies=("search_web:address_bar_type_enter",),
        fallback_strategies=(
            "search_web:dom_search_input",
            "search_web:keyboard_focus_then_type",
        ),
        required_params=("query",),
        optional_params=("engine", "target"),
        outcome_validator=_need_query,
    ),
    "select_visible_option": UICCapability(
        capability_id="select_visible_option",
        family=UIC_FAMILY_BUTTONS_MENUS_OPTIONS,
        description="Elegir opción visible (lista/menú)",
        semantic_type="select",
        preferred_strategies=("select_option:uia_text_match",),
        outcome_validator=_passthrough_validator,
    ),
    "select_entity_from_collection": UICCapability(
        capability_id="select_entity_from_collection",
        family=UIC_FAMILY_BUTTONS_MENUS_OPTIONS,
        description="Seleccionar entidad dentro de colección visible (UCES)",
        semantic_type="entity_in_collection",
        preferred_strategies=("select_entity:collection_first",),
        fallback_strategies=(
            "select_entity:operational_entity_match",
            "select_entity:legacy_kind_fallback",
        ),
        optional_params=(
            "operational_collection", "collection_entity", "operational_entity",
            "selection_label", "selection_confidence", "candidate_count",
            "fallback_legacy_type",
        ),
        outcome_validator=_passthrough_validator,
    ),
    "select_profile": UICCapability(
        capability_id="select_profile",
        family=UIC_FAMILY_AUTHENTICATION,
        description="Seleccionar perfil de navegador",
        semantic_type="profile_pick",
        preferred_strategies=("select_profile:uia_text_match",),
        fallback_strategies=(
            "select_profile:visible_text_ocr",
            "select_profile:chrome_profile_alias",
            "select_profile:skip_if_loaded",
        ),
        required_params=(),
        optional_params=("profile_name", "profile", "app"),
        outcome_validator=_need_profile,
    ),
    "use_selected_profile": UICCapability(
        capability_id="use_selected_profile",
        family=UIC_FAMILY_AUTHENTICATION,
        description="Continuar con perfil ya activo",
        semantic_type="profile_skip",
        preferred_strategies=("select_profile:skip_if_loaded",),
        outcome_validator=_passthrough_validator,
    ),
    "scroll_results": UICCapability(
        capability_id="scroll_results",
        family=UIC_FAMILY_BROWSER,
        description="Scroll en resultados/listados",
        semantic_type="scroll",
        preferred_strategies=("scroll_results:wheel",),
        fallback_strategies=("scroll_results:keyboard",),
        optional_params=("direction", "amount", "context"),
        outcome_validator=_passthrough_validator,
    ),
    "scroll_page": UICCapability(
        capability_id="scroll_page",
        family=UIC_FAMILY_BROWSER,
        description="Scroll genérico en página",
        semantic_type="scroll",
        preferred_strategies=("scroll_page:wheel",),
        outcome_validator=_passthrough_validator,
    ),
    "copy_selection": UICCapability(
        capability_id="copy_selection",
        family=UIC_FAMILY_CLIPBOARD_DATA,
        description="Copiar selección al portapapeles",
        semantic_type="clipboard",
        preferred_strategies=("clipboard:ctrl_c",),
        outcome_validator=_passthrough_validator,
        hotkey_first=True,
    ),
    "paste_clipboard": UICCapability(
        capability_id="paste_clipboard",
        family=UIC_FAMILY_CLIPBOARD_DATA,
        description="Pegar portapapeles",
        semantic_type="clipboard",
        preferred_strategies=("clipboard:ctrl_v",),
        outcome_validator=_passthrough_validator,
        hotkey_first=True,
    ),
    "store_clipboard_snapshot": UICCapability(
        capability_id="store_clipboard_snapshot",
        family=UIC_FAMILY_CLIPBOARD_DATA,
        description="Guardar snapshot seguro del portapapeles",
        semantic_type="clipboard_snapshot",
        preferred_strategies=("clipboard:snapshot_store",),
        outcome_validator=_passthrough_validator,
    ),
    "restore_clipboard_snapshot": UICCapability(
        capability_id="restore_clipboard_snapshot",
        family=UIC_FAMILY_CLIPBOARD_DATA,
        description="Restaurar snapshot del portapapeles",
        semantic_type="clipboard_snapshot",
        preferred_strategies=("clipboard:snapshot_restore",),
        outcome_validator=_passthrough_validator,
    ),
    "wait_until": UICCapability(
        capability_id="wait_until",
        family=UIC_FAMILY_TEMPORAL_WAITS,
        description="Esperar condición estable",
        semantic_type="wait",
        preferred_strategies=("wait_for:state",),
        outcome_validator=_passthrough_validator,
    ),
    "wait_for": UICCapability(
        capability_id="wait_for",
        family=UIC_FAMILY_TEMPORAL_WAITS,
        description="Alias legacy de espera",
        semantic_type="wait",
        preferred_strategies=("wait_for:state",),
        outcome_validator=_passthrough_validator,
    ),
    "confirm": UICCapability(
        capability_id="confirm",
        family=UIC_FAMILY_WORKFLOW_SEMANTICS,
        description="Confirmación de workflow",
        semantic_type="confirm",
        preferred_strategies=("confirm:ui",),
        outcome_validator=_passthrough_validator,
    ),
    "validate_outcome": UICCapability(
        capability_id="validate_outcome",
        family=UIC_FAMILY_VALIDATION_OUTCOME,
        description="Validar resultado esperado del paso",
        semantic_type="validate",
        preferred_strategies=("validate_outcome:state_diff",),
        outcome_validator=_passthrough_validator,
    ),
    "self_heal_target": UICCapability(
        capability_id="self_heal_target",
        family=UIC_FAMILY_SELF_HEALING,
        description="Autocuración de target semántico",
        semantic_type="heal",
        preferred_strategies=("self_heal_target:reconcile",),
        outcome_validator=_passthrough_validator,
        supports_rerecord=False,
    ),
    "rerecord_step": UICCapability(
        capability_id="rerecord_step",
        family=UIC_FAMILY_RERECORD_REPAIR,
        description="Regrabar un único paso con overlay seguro",
        semantic_type="rerecord",
        preferred_strategies=("rerecord_step:intent_layer",),
        outcome_validator=_passthrough_validator,
        allows_coords_emergency=False,
    ),
    "repair_step": UICCapability(
        capability_id="repair_step",
        family=UIC_FAMILY_RERECORD_REPAIR,
        description="Reparar paso débil encadenando cola",
        semantic_type="repair",
        preferred_strategies=("repair_step:rerecord_chain",),
        outcome_validator=_passthrough_validator,
    ),
    "open_site": UICCapability(
        capability_id="open_site",
        family=UIC_FAMILY_BROWSER,
        description="Abrir sitio genérico por nombre",
        semantic_type="navigate",
        preferred_strategies=("open_site:smart_route",),
        fallback_strategies=("open_url:hotkey_ctrl_l",),
        outcome_validator=_passthrough_validator,
    ),
    "fill_form": UICCapability(
        capability_id="fill_form",
        family=UIC_FAMILY_FIELDS_TEXT,
        description="Secuencia de campos de formulario semanticos",
        semantic_type="input",
        preferred_strategies=("fill_form:semantic_focus_chain",),
        fallback_strategies=("type_field_value:keyboard",),
        outcome_validator=_passthrough_validator,
    ),
    "submit_form": UICCapability(
        capability_id="submit_form",
        family=UIC_FAMILY_FIELDS_TEXT,
        description="Confirmar envío del formulario",
        semantic_type="submit",
        preferred_strategies=("submit_form:confirm",),
        fallback_strategies=("submit_field:enter",),
        outcome_validator=_passthrough_validator,
        hotkey_first=True,
    ),
    "switch_context": UICCapability(
        capability_id="switch_context",
        family=UIC_FAMILY_WORKFLOW_SEMANTICS,
        description="Cambio deliberado de contexto foreground",
        semantic_type="context_switch",
        preferred_strategies=("switch_context:focus_foreground",),
        fallback_strategies=("wait_for:state",),
        outcome_validator=_passthrough_validator,
    ),
    "confirm_dialog": UICCapability(
        capability_id="confirm_dialog",
        family=UIC_FAMILY_WORKFLOW_SEMANTICS,
        description="Responder a diálogo modal",
        semantic_type="confirm",
        preferred_strategies=("confirm_dialog:dismiss_positive",),
        fallback_strategies=("confirm:ui",),
        outcome_validator=_passthrough_validator,
    ),
    "choose_option": UICCapability(
        capability_id="choose_option",
        family=UIC_FAMILY_BUTTONS_MENUS_OPTIONS,
        description="Elegir opción explícita en UI",
        semantic_type="select",
        preferred_strategies=("choose_option:list_select",),
        fallback_strategies=(
            "select_visible_option:list_activation",
            "select_option:uia_text_match",
        ),
        outcome_validator=_passthrough_validator,
    ),
    "web_search": UICCapability(
        capability_id="web_search",
        family=UIC_FAMILY_BROWSER,
        description="Búsqueda web genérica",
        semantic_type="search",
        preferred_strategies=("web_search:smart_route",),
        outcome_validator=_need_query,
    ),
    "open_search_result": UICCapability(
        capability_id="open_search_result",
        family=UIC_FAMILY_BROWSER,
        description="Abrir resultado de SERP por título",
        semantic_type="navigate",
        preferred_strategies=("open_search_result:dom_link_by_title",),
        outcome_validator=_passthrough_validator,
    ),
    # Legacy explícito — no aparece en SEP tras migración UIC.
    "search_youtube": UICCapability(
        capability_id="search_youtube",
        family=UIC_FAMILY_BROWSER,
        description="[legacy] Búsqueda YouTube — usar search_content",
        semantic_type="search",
        preferred_strategies=("search_youtube:dom_input",),
        fallback_strategies=(),
        legacy_alias=True,
        required_params=("query",),
        optional_params=("site", "target"),
        outcome_validator=_need_query,
    ),
}


def get_capability(capability_id: str) -> Optional[UICCapability]:
    return _CAPABILITIES.get(str(capability_id or "").strip())


def all_registered_capability_ids() -> FrozenSet[str]:
    return frozenset(_CAPABILITIES.keys())


def validate_step_against_registry(step: Dict[str, Any]) -> Optional[str]:
    """Devuelve mensaje de error humano o None si OK."""
    if not isinstance(step, dict) or not step:
        return None
    raw_t = step.get("type")
    if not isinstance(raw_t, str) or not raw_t.strip():
        return None
    cap = get_capability(raw_t.strip())
    if cap is None:
        return f"UNKNOWN_UIC_CAPABILITY:{raw_t.strip()}"
    err = cap.outcome_validator(step)
    if err:
        return f"{cap.capability_id}:{err}"
    return None


__all__ = [
    "UICCapability",
    "UIC_FAMILIES",
    "get_capability",
    "all_registered_capability_ids",
    "validate_step_against_registry",
]
