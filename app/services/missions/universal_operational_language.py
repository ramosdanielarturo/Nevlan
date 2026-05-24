"""Universal Operational Language (UOL) — Nevlan operational intent layer.

Nevlan deja de pensar ``click → route → maybe infer → maybe ask`` y pasa a:

    INTENT · SURFACE · TARGET · INPUT · EXPECTED OUTCOME · VALIDATORS · CONTRACT

FFEC/COI aporta verdad canónica pre-transición; UOL la compila en acciones
operacionales universales ejecutables sin vocabulario de producto (Chrome,
YouTube, profile picker, ``smart_route``, etc.).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.mission import (
    CanonicalOperationalIntent,
    OperationalAffordanceType,
    OperationalEntityType,
    OperationalFreezeEntityCandidate,
)

# ── Universal action vocabulary ─────────────────────────────────────────────


class UniversalOperationalActionType(str, Enum):
    """Acciones operacionales universales (agnósticas de app/sitio)."""

    OPEN_APPLICATION = "open_application"
    OPEN_TAB = "open_tab"
    NAVIGATE_TO_LOCATION = "navigate_to_location"
    SEARCH_CONTENT = "search_content"
    SELECT_ENTITY_FROM_COLLECTION = "select_entity_from_collection"
    BROWSE_RESULTS = "browse_results"
    FILL_FIELD = "fill_field"
    SUBMIT_INPUT = "submit_input"
    SWITCH_CONTEXT = "switch_context"
    CLOSE_CONTEXT = "close_context"
    CONFIRM_ACTION = "confirm_action"
    WAIT_FOR_DYNAMIC_STATE = "wait_for_dynamic_state"
    SCROLL_RESULTS = "scroll_results"


class OperationalSurfaceType(str, Enum):
    """Superficie donde se ejecuta la acción (sin hardcode de producto)."""

    APPLICATION_LAUNCHER = "application_launcher"
    SEARCH_SURFACE = "search_surface"
    COLLECTION_SURFACE = "collection_surface"
    NAVIGATION_SURFACE = "navigation_surface"
    TAB_SURFACE = "tab_surface"
    FORM_SURFACE = "form_surface"
    RESULTS_SURFACE = "results_surface"
    GENERIC_SURFACE = "generic_surface"


class OperationalOutcomeExpectation(BaseModel):
    """Resultado observable esperado tras ejecutar la acción."""

    model_config = ConfigDict(extra="ignore")

    results_visible: bool = False
    query_visible: bool = False
    entity_selected: bool = False
    location_reached: bool = False
    tab_opened: bool = False
    application_focused: bool = False
    dynamic_state_settled: bool = False
    context_switched: bool = False
    extra: Dict[str, Any] = Field(default_factory=dict)


class OperationalActionGrammar(BaseModel):
    """Gramática declarativa: qué inputs y superficie requiere la acción."""

    model_config = ConfigDict(extra="ignore")

    required_inputs: List[str] = Field(default_factory=list)
    optional_inputs: List[str] = Field(default_factory=list)
    surface_type: OperationalSurfaceType = OperationalSurfaceType.GENERIC_SURFACE
    allows_empty_target: bool = False


class OperationalExecutionContract(BaseModel):
    """Contrato de ejecución segura: pasos, validadores y recuperación."""

    model_config = ConfigDict(extra="ignore")

    locate_input_surface: bool = False
    inject_text: bool = False
    submit: bool = False
    validate_results: bool = False
    locate_collection: bool = False
    select_entity: bool = False
    validate_entity_selected: bool = False
    focus_surface: bool = False
    navigate: bool = False
    validate_location: bool = False
    open_application: bool = False
    validate_application_focus: bool = False
    open_tab: bool = False
    wait_for_transition: bool = False
    scroll_results: bool = False

    execution_steps: List[str] = Field(default_factory=list)
    validators: List[str] = Field(default_factory=list)
    fallback_policies: List[str] = Field(default_factory=list)
    dynamic_recovery_rules: List[str] = Field(default_factory=list)
    preferred_runtime_strategies: List[str] = Field(default_factory=list)
    fallback_runtime_strategies: List[str] = Field(default_factory=list)


class UniversalOperationalAction(BaseModel):
    """Acción operacional universal compilada desde COI o paso semántico."""

    model_config = ConfigDict(extra="ignore")

    uol_action: UniversalOperationalActionType
    surface_type: OperationalSurfaceType = OperationalSurfaceType.GENERIC_SURFACE

    target_entity_type: str = ""
    target_display_name: str = ""
    target_normalized_name: str = ""
    input_text: str = ""
    location_hint: str = ""
    collection_type: str = ""

    expected_outcome: OperationalOutcomeExpectation = Field(
        default_factory=OperationalOutcomeExpectation,
    )
    grammar: OperationalActionGrammar = Field(
        default_factory=OperationalActionGrammar,
    )
    execution_contract: OperationalExecutionContract = Field(
        default_factory=OperationalExecutionContract,
    )

    source: str = ""  # "coi" | "semantic_step" | "legacy_step"
    canonical_truth: bool = False
    coi_intent_id: str = ""
    source_event_id: str = ""
    entity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    def to_step_params(self) -> Dict[str, Any]:
        """Serializa UOL en ``MissionStep.params`` para el runtime."""
        return {
            "uol_action": self.uol_action.value,
            "uol_surface_type": self.surface_type.value,
            "uol_target_entity_type": self.target_entity_type,
            "uol_target_display_name": self.target_display_name,
            "uol_input_text": self.input_text,
            "uol_location_hint": self.location_hint,
            "uol_canonical_truth": self.canonical_truth,
            "uol_expected_outcome": self.expected_outcome.model_dump(mode="json"),
            "uol_execution_contract": self.execution_contract.model_dump(mode="json"),
            "preferred_strategy": (
                self.execution_contract.preferred_runtime_strategies[0]
                if self.execution_contract.preferred_runtime_strategies
                else ""
            ),
            "fallback_strategies": list(
                self.execution_contract.fallback_runtime_strategies,
            ),
        }


# ── Technical noise (never in normal Mission Review) ───────────────────────

_TECHNICAL_LABEL_MARKERS: Tuple[str, ...] = (
    "smart_route",
    "uia_text_match",
    "click_visual",
    "collection_first",
    "wheel",
    "ctrl+l",
    "ctrl+t",
    "coords",
    "relative_coords",
    "visual_asset",
    "elemento identificado automáticamente",
)

_ENTITY_TYPE_PROFILE_MARKERS: Tuple[str, ...] = (
    "user_profile",
    "browser_profile",
    "profile",
)


def normalize_display_name(raw: str) -> str:
    """Normaliza nombres de entidad para copy humano.

    ``Abrir el perfil de Daniel Chrome`` → ``Daniel Chrome``
    """
    if not raw:
        return ""
    s = str(raw).strip()
    patterns = (
        r"^(?:abrir|seleccionar|usar|elegir)\s+(?:el\s+)?(?:perfil\s+de\s+)?",
        r"^(?:open|select|use)\s+(?:the\s+)?(?:profile\s+)?",
        r"^perfil\s+",
    )
    for pat in patterns:
        s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()
    return s.strip()


def _entity_type_from_candidate(
    ent: Optional[OperationalFreezeEntityCandidate],
) -> str:
    if ent is None:
        return ""
    hint = str(getattr(ent, "entity_type_hint", "") or "").strip().lower()
    if hint:
        return hint
    name = str(ent.display_name or "").lower()
    if "perfil" in name or "profile" in name:
        return OperationalEntityType.USER_PROFILE.value
    return OperationalEntityType.UNKNOWN.value


def _is_profile_entity(entity_type: str, display_name: str) -> bool:
    et = (entity_type or "").lower()
    if any(m in et for m in _ENTITY_TYPE_PROFILE_MARKERS):
        return True
    low = (display_name or "").lower()
    return "perfil" in low or "profile" in low


# ── Execution contract builder ────────────────────────────────────────────────


def build_universal_execution_contract(
    action: UniversalOperationalAction,
) -> OperationalExecutionContract:
    """Genera contrato ejecutable: pasos, validadores y políticas de fallback."""
    uol = action.uol_action
    contract = OperationalExecutionContract()

    if uol == UniversalOperationalActionType.SEARCH_CONTENT:
        contract.locate_input_surface = True
        contract.focus_surface = True
        contract.inject_text = True
        contract.submit = True
        contract.validate_results = True
        contract.wait_for_transition = True
        contract.execution_steps = [
            "localizar_superficie_busqueda",
            "focus",
            "inject_text",
            "submit",
            "esperar_cambio_resultados",
            "validar_query_visible",
            "validar_contexto_consistente",
        ]
        contract.validators = [
            "query_visible",
            "results_visible",
            "context_consistent",
        ]
        contract.fallback_policies = [
            "retry_submit",
            "refocus_search_surface",
            "legacy_search_fallback",
        ]
        contract.dynamic_recovery_rules = [
            "wait_for_dynamic_results",
            "revalidate_after_transition",
        ]
        contract.preferred_runtime_strategies = [
            f"search_content:uol_surface",
        ]
        contract.fallback_runtime_strategies = [
            "search_youtube:dom_input",
            "search_web:address_bar_type_enter",
        ]

    elif uol == UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION:
        contract.locate_collection = True
        contract.select_entity = True
        contract.validate_entity_selected = True
        contract.wait_for_transition = True
        contract.execution_steps = [
            "localizar_coleccion",
            "resolver_entidad_por_nombre",
            "seleccionar_entidad",
            "validar_entidad_seleccionada",
            "validar_transicion_esperada",
        ]
        contract.validators = [
            "entity_selected",
            "collection_state_consistent",
        ]
        contract.fallback_policies = [
            "scroll_collection",
            "uia_entity_match",
            "legacy_collection_fallback",
        ]
        contract.dynamic_recovery_rules = [
            "revalidate_after_collection_transition",
        ]
        contract.preferred_runtime_strategies = [
            "select_entity_from_collection:uol_entity",
        ]
        contract.fallback_runtime_strategies = [
            "select_entity_from_collection:uia_name",
            "select_profile:uia_text_match",
        ]

    elif uol == UniversalOperationalActionType.OPEN_APPLICATION:
        contract.open_application = True
        contract.validate_application_focus = True
        contract.execution_steps = [
            "localizar_lanzador",
            "focus_launcher",
            "inject_application_name",
            "submit_launch",
            "validar_aplicacion_en_foco",
        ]
        contract.validators = ["application_focused"]
        contract.fallback_policies = ["process_launch", "legacy_open_app"]
        contract.preferred_runtime_strategies = ["open_application:uol_launcher"]
        contract.fallback_runtime_strategies = [
            "open_app:os_startfile",
            "open_app:windows_search",
        ]

    elif uol == UniversalOperationalActionType.OPEN_TAB:
        contract.open_tab = True
        contract.execution_steps = ["hotkey_new_tab", "validar_pestaña_nueva"]
        contract.validators = ["tab_opened"]
        contract.preferred_runtime_strategies = ["open_tab:uol_hotkey"]
        contract.fallback_runtime_strategies = [
            "open_new_tab:hotkey_ctrl_t",
            "open_new_tab:uia_button",
        ]

    elif uol == UniversalOperationalActionType.NAVIGATE_TO_LOCATION:
        contract.navigate = True
        contract.focus_surface = True
        contract.inject_text = True
        contract.submit = True
        contract.validate_location = True
        contract.execution_steps = [
            "focus_navigation_surface",
            "inject_location",
            "submit_navigation",
            "validar_ubicacion",
        ]
        contract.validators = ["location_reached"]
        contract.preferred_runtime_strategies = ["navigate_to_location:uol_address_surface"]
        contract.fallback_runtime_strategies = [
            "open_url:hotkey_ctrl_l",
            "open_url:playwright_goto",
        ]

    elif uol == UniversalOperationalActionType.SCROLL_RESULTS:
        contract.scroll_results = True
        contract.execution_steps = ["scroll_results_surface"]
        contract.validators = ["results_scrolled"]
        contract.preferred_runtime_strategies = ["browse_results:uol_scroll_surface"]
        contract.fallback_runtime_strategies = [
            "scroll_results:keyboard",
            "scroll_results:js_scrollby",
        ]

    elif uol == UniversalOperationalActionType.FILL_FIELD:
        contract.locate_input_surface = True
        contract.inject_text = True
        contract.execution_steps = [
            "locate_field",
            "focus_field",
            "inject_text",
            "validate_field_value",
        ]
        contract.validators = ["field_value_set"]
        contract.preferred_runtime_strategies = ["fill_form:uol_fields"]
        contract.fallback_runtime_strategies = [
            "fill_field:uol_field",
            "search_web:keyboard_focus_then_type",
        ]

    elif uol == UniversalOperationalActionType.SUBMIT_INPUT:
        contract.submit = True
        contract.execution_steps = [
            "locate_submit_control",
            "activate_submit",
            "validate_outcome",
        ]
        contract.validators = ["submitted"]
        contract.preferred_runtime_strategies = ["submit_form:uol_submit"]
        contract.fallback_runtime_strategies = ["submit_input:uol_submit"]

    elif uol == UniversalOperationalActionType.SWITCH_CONTEXT:
        contract.execution_steps = [
            "locate_target_context",
            "activate_context",
            "validate_context_switch",
        ]
        contract.validators = ["context_switched"]
        contract.preferred_runtime_strategies = ["switch_context:uol_context"]
        contract.fallback_runtime_strategies = ["wait_for_state:poll"]

    elif uol == UniversalOperationalActionType.CONFIRM_ACTION:
        contract.execution_steps = [
            "locate_dialog",
            "activate_choice",
            "validate_dialog_dismissed",
        ]
        contract.validators = ["dialog_dismissed"]
        contract.preferred_runtime_strategies = ["confirm_dialog:uol_dialog"]
        contract.fallback_runtime_strategies = ["submit_input:uol_submit"]

    elif uol == UniversalOperationalActionType.WAIT_FOR_DYNAMIC_STATE:
        contract.wait_for_transition = True
        contract.execution_steps = ["wait_for_dynamic_state"]
        contract.validators = ["dynamic_state_settled"]
        contract.preferred_runtime_strategies = ["wait_for_state:poll"]

    else:
        contract.execution_steps = [f"execute_{uol.value}"]
        contract.validators = ["outcome_met"]
        contract.fallback_policies = ["legacy_fallback"]

    action.execution_contract = contract
    return contract


# ── COI → UOL compilation ───────────────────────────────────────────────────


def _uol_type_from_coi_affordance(coi: CanonicalOperationalIntent) -> UniversalOperationalActionType:
    """Mapea UOAC → acción UOL (nunca asume select por etiqueta UIA)."""
    aff = coi.operational_affordance
    hint = str(aff.uol_action_hint if aff else "") or str(coi.operational_intent_kind or "")
    mapping = {
        "open_application": UniversalOperationalActionType.OPEN_APPLICATION,
        "open_tab": UniversalOperationalActionType.OPEN_TAB,
        "navigate_to_location": UniversalOperationalActionType.NAVIGATE_TO_LOCATION,
        "select_entity_from_collection": (
            UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION
        ),
        "fill_field": UniversalOperationalActionType.FILL_FIELD,
        "search_content": UniversalOperationalActionType.SEARCH_CONTENT,
        "submit_input": UniversalOperationalActionType.SUBMIT_INPUT,
        "scroll_results": UniversalOperationalActionType.SCROLL_RESULTS,
        "switch_context": UniversalOperationalActionType.SWITCH_CONTEXT,
        "confirm_action": UniversalOperationalActionType.CONFIRM_ACTION,
        "close_context": UniversalOperationalActionType.CLOSE_CONTEXT,
    }
    if hint in mapping:
        return mapping[hint]
    if aff:
        by_type = {
            OperationalAffordanceType.ACTIVATE_LAUNCHER: (
                UniversalOperationalActionType.OPEN_APPLICATION
            ),
            OperationalAffordanceType.CREATE_NEW_CONTEXT: (
                UniversalOperationalActionType.OPEN_TAB
            ),
            OperationalAffordanceType.NAVIGATE_TO_RESOURCE: (
                UniversalOperationalActionType.NAVIGATE_TO_LOCATION
            ),
            OperationalAffordanceType.SELECT_IDENTITY_CONTEXT: (
                UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION
            ),
            OperationalAffordanceType.CHOOSE_OPTION: (
                UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION
            ),
            OperationalAffordanceType.FOCUS_INPUT: UniversalOperationalActionType.FILL_FIELD,
            OperationalAffordanceType.SEARCH_WITH_QUERY: (
                UniversalOperationalActionType.SEARCH_CONTENT
            ),
            OperationalAffordanceType.SUBMIT_INPUT: UniversalOperationalActionType.SUBMIT_INPUT,
            OperationalAffordanceType.BROWSE_CONTENT: (
                UniversalOperationalActionType.SCROLL_RESULTS
            ),
            OperationalAffordanceType.OPEN_MENU: UniversalOperationalActionType.SWITCH_CONTEXT,
            OperationalAffordanceType.CONFIRM_ACTION: (
                UniversalOperationalActionType.CONFIRM_ACTION
            ),
            OperationalAffordanceType.DISMISS_DIALOG: (
                UniversalOperationalActionType.CLOSE_CONTEXT
            ),
        }
        return by_type.get(
            aff.affordance_type,
            UniversalOperationalActionType.WAIT_FOR_DYNAMIC_STATE,
        )
    return UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION


def compile_canonical_intent_to_uol(
    coi: CanonicalOperationalIntent,
) -> UniversalOperationalAction:
    """Compila COI (FFEC + UOAC) en acción UOL."""
    ent = coi.entity
    coll = coi.collection
    aff = coi.operational_affordance
    uol_type = _uol_type_from_coi_affordance(coi)

    display = normalize_display_name(ent.display_name if ent else "")
    entity_type = _entity_type_from_candidate(ent)
    conf = float(
        (ent.confidence if ent else 0.0) or coi.truth_strength or coi.freeze_confidence,
    )

    surface = OperationalSurfaceType.GENERIC_SURFACE
    if uol_type == UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION:
        surface = OperationalSurfaceType.COLLECTION_SURFACE
    elif uol_type in (
        UniversalOperationalActionType.SEARCH_CONTENT,
        UniversalOperationalActionType.FILL_FIELD,
    ):
        surface = OperationalSurfaceType.SEARCH_SURFACE
    elif uol_type == UniversalOperationalActionType.OPEN_APPLICATION:
        surface = OperationalSurfaceType.APPLICATION_LAUNCHER
    elif uol_type == UniversalOperationalActionType.OPEN_TAB:
        surface = OperationalSurfaceType.TAB_SURFACE
    elif uol_type == UniversalOperationalActionType.NAVIGATE_TO_LOCATION:
        surface = OperationalSurfaceType.NAVIGATION_SURFACE
    elif uol_type == UniversalOperationalActionType.SCROLL_RESULTS:
        surface = OperationalSurfaceType.RESULTS_SURFACE

    if uol_type != UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION:
        display = ""
        entity_type = ""

    action = UniversalOperationalAction(
        uol_action=uol_type,
        surface_type=surface,
        target_entity_type=entity_type,
        target_display_name=display or (ent.display_name if ent else ""),
        target_normalized_name=(
            ent.normalized_name if ent else normalize_display_name(display)
        ),
        collection_type=str(coll.collection_type if coll else ""),
        expected_outcome=OperationalOutcomeExpectation(
            entity_selected=(
                uol_type == UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION
            ),
            tab_opened=(uol_type == UniversalOperationalActionType.OPEN_TAB),
            application_focused=(
                uol_type == UniversalOperationalActionType.OPEN_APPLICATION
            ),
            location_reached=(
                uol_type == UniversalOperationalActionType.NAVIGATE_TO_LOCATION
            ),
            results_visible=(
                uol_type == UniversalOperationalActionType.SCROLL_RESULTS
            ),
            query_visible=(
                uol_type == UniversalOperationalActionType.SEARCH_CONTENT
            ),
            dynamic_state_settled=bool(coi.transition_expected),
        ),
        grammar=OperationalActionGrammar(
            required_inputs=(
                ["target_display_name"]
                if uol_type == UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION
                else []
            ),
            surface_type=surface,
        ),
        source="coi_uoac",
        canonical_truth=bool(coi.canonical_truth),
        coi_intent_id=str(coi.intent_id or ""),
        source_event_id=str(coi.source_event_id or ""),
        entity_confidence=conf,
    )
    if aff and aff.human_label_hint:
        action.location_hint = aff.human_label_hint
    build_universal_execution_contract(action)
    return action


# ── Semantic step → UOL ───────────────────────────────────────────────────

_LEGACY_TYPE_TO_UOL: Dict[str, UniversalOperationalActionType] = {
    "open_app": UniversalOperationalActionType.OPEN_APPLICATION,
    "open_new_tab": UniversalOperationalActionType.OPEN_TAB,
    "open_url": UniversalOperationalActionType.NAVIGATE_TO_LOCATION,
    "open_site": UniversalOperationalActionType.NAVIGATE_TO_LOCATION,
    "open_bookmark": UniversalOperationalActionType.NAVIGATE_TO_LOCATION,
    "search": UniversalOperationalActionType.SEARCH_CONTENT,
    "search_content": UniversalOperationalActionType.SEARCH_CONTENT,
    "search_site": UniversalOperationalActionType.SEARCH_CONTENT,
    "search_youtube": UniversalOperationalActionType.SEARCH_CONTENT,
    "search_web": UniversalOperationalActionType.SEARCH_CONTENT,
    "web_search": UniversalOperationalActionType.SEARCH_CONTENT,
    "select_profile": UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION,
    "select_entity_from_collection": (
        UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION
    ),
    "scroll_page": UniversalOperationalActionType.SCROLL_RESULTS,
    "scroll_results": UniversalOperationalActionType.SCROLL_RESULTS,
    "browse_results": UniversalOperationalActionType.SCROLL_RESULTS,
    "wait_for_state": UniversalOperationalActionType.WAIT_FOR_DYNAMIC_STATE,
    "fill_field": UniversalOperationalActionType.FILL_FIELD,
    "fill_form": UniversalOperationalActionType.FILL_FIELD,
    "submit_form": UniversalOperationalActionType.SUBMIT_INPUT,
    "submit_input": UniversalOperationalActionType.SUBMIT_INPUT,
    "switch_context": UniversalOperationalActionType.SWITCH_CONTEXT,
    "confirm_dialog": UniversalOperationalActionType.CONFIRM_ACTION,
}


def compile_semantic_step_to_uol(
    step: Dict[str, Any],
    *,
    coi: Optional[CanonicalOperationalIntent] = None,
) -> Optional[UniversalOperationalAction]:
    """Compila un paso del plan semántico (SEP) a UOL."""
    if coi and coi.canonical_truth:
        return compile_canonical_intent_to_uol(coi)

    t = str(step.get("type") or "").strip()
    params = dict(step.get("params") or {})
    uol_type = _LEGACY_TYPE_TO_UOL.get(t)
    if uol_type is None:
        return None

    action = UniversalOperationalAction(
        uol_action=uol_type,
        source="semantic_step",
        canonical_truth=False,
    )

    if uol_type == UniversalOperationalActionType.OPEN_APPLICATION:
        action.surface_type = OperationalSurfaceType.APPLICATION_LAUNCHER
        action.target_display_name = str(
            params.get("name") or params.get("app") or "",
        ).strip()
        action.grammar = OperationalActionGrammar(
            required_inputs=["target_display_name"],
            surface_type=OperationalSurfaceType.APPLICATION_LAUNCHER,
        )
        action.expected_outcome = OperationalOutcomeExpectation(
            application_focused=True,
        )

    elif uol_type == UniversalOperationalActionType.OPEN_TAB:
        action.surface_type = OperationalSurfaceType.TAB_SURFACE
        action.expected_outcome = OperationalOutcomeExpectation(tab_opened=True)

    elif uol_type == UniversalOperationalActionType.NAVIGATE_TO_LOCATION:
        action.surface_type = OperationalSurfaceType.NAVIGATION_SURFACE
        action.location_hint = str(
            params.get("url") or params.get("alias") or params.get("site") or "",
        ).strip()
        action.grammar = OperationalActionGrammar(
            required_inputs=["location_hint"],
            surface_type=OperationalSurfaceType.NAVIGATION_SURFACE,
        )
        action.expected_outcome = OperationalOutcomeExpectation(
            location_reached=True,
        )

    elif uol_type == UniversalOperationalActionType.SEARCH_CONTENT:
        action.surface_type = OperationalSurfaceType.SEARCH_SURFACE
        action.input_text = str(
            params.get("query") or params.get("val") or params.get("text") or "",
        ).strip()
        action.grammar = OperationalActionGrammar(
            required_inputs=["input_text"],
            surface_type=OperationalSurfaceType.SEARCH_SURFACE,
        )
        action.expected_outcome = OperationalOutcomeExpectation(
            results_visible=True,
            query_visible=True,
        )

    elif uol_type == UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION:
        ent_raw = params.get("collection_entity") or params.get("entity") or {}
        name = ""
        etype = ""
        if isinstance(ent_raw, dict):
            name = str(ent_raw.get("display_name") or "").strip()
            etype = str(ent_raw.get("entity_type") or "").strip()
        name = name or str(
            params.get("profile")
            or params.get("profile_name")
            or params.get("selection_label")
            or "",
        ).strip()
        action.surface_type = OperationalSurfaceType.COLLECTION_SURFACE
        action.target_display_name = normalize_display_name(name) or name
        action.target_entity_type = etype or (
            OperationalEntityType.USER_PROFILE.value
            if t == "select_profile"
            else ""
        )
        action.entity_confidence = float(
            params.get("selection_confidence")
            or params.get("freeze_confidence")
            or params.get("entity_resolution_confidence")
            or 0.0,
        )
        action.canonical_truth = bool(params.get("canonical_truth"))
        action.expected_outcome = OperationalOutcomeExpectation(
            entity_selected=True,
        )

    elif uol_type == UniversalOperationalActionType.SCROLL_RESULTS:
        action.surface_type = OperationalSurfaceType.RESULTS_SURFACE
        action.expected_outcome = OperationalOutcomeExpectation(results_visible=True)

    elif uol_type == UniversalOperationalActionType.FILL_FIELD:
        action.surface_type = OperationalSurfaceType.FORM_SURFACE
        action.input_text = str(
            params.get("uol_input_text")
            or params.get("text")
            or params.get("value")
            or params.get("content")
            or params.get("multiline_text")
            or "",
        ).strip()
        action.location_hint = str(
            params.get("field")
            or params.get("target")
            or params.get("target_label")
            or params.get("label")
            or params.get("name")
            or "",
        ).strip()
        action.grammar = OperationalActionGrammar(
            required_inputs=["input_text"] if action.input_text else [],
            surface_type=OperationalSurfaceType.FORM_SURFACE,
        )
        action.expected_outcome = OperationalOutcomeExpectation(
            extra={"field_value_set": bool(action.input_text)},
        )

    elif uol_type == UniversalOperationalActionType.SUBMIT_INPUT:
        action.surface_type = OperationalSurfaceType.FORM_SURFACE
        action.location_hint = str(
            params.get("action")
            or params.get("method")
            or params.get("button")
            or "",
        ).strip()
        action.grammar = OperationalActionGrammar(
            surface_type=OperationalSurfaceType.FORM_SURFACE,
        )
        action.expected_outcome = OperationalOutcomeExpectation(
            extra={
                "submit_action": action.location_hint,
                "path": str(
                    params.get("path")
                    or params.get("filename")
                    or params.get("file")
                    or "",
                ).strip(),
            },
        )

    elif uol_type == UniversalOperationalActionType.SWITCH_CONTEXT:
        action.surface_type = OperationalSurfaceType.GENERIC_SURFACE
        action.location_hint = str(
            params.get("action")
            or params.get("method")
            or "",
        ).strip()
        action.target_display_name = str(
            params.get("target")
            or params.get("window")
            or params.get("title")
            or params.get("name")
            or "",
        ).strip()
        action.input_text = str(
            params.get("text")
            or params.get("mark_dirty")
            or "",
        ).strip()
        action.grammar = OperationalActionGrammar(
            surface_type=OperationalSurfaceType.GENERIC_SURFACE,
        )
        action.expected_outcome = OperationalOutcomeExpectation(
            context_switched=True,
        )

    elif uol_type == UniversalOperationalActionType.CONFIRM_ACTION:
        action.surface_type = OperationalSurfaceType.GENERIC_SURFACE
        action.location_hint = str(
            params.get("choice")
            or params.get("answer")
            or params.get("action")
            or "",
        ).strip()
        action.target_display_name = str(
            params.get("button")
            or params.get("label")
            or "",
        ).strip()
        action.grammar = OperationalActionGrammar(
            surface_type=OperationalSurfaceType.GENERIC_SURFACE,
        )
        action.expected_outcome = OperationalOutcomeExpectation(
            extra={"dialog_dismissed": True},
        )

    build_universal_execution_contract(action)
    return action


def uol_from_step_params(params: Dict[str, Any]) -> Optional[UniversalOperationalAction]:
    """Reconstruye UOL desde ``MissionStep.params`` (post-adaptación)."""
    raw_action = str(params.get("uol_action") or "").strip()
    if not raw_action:
        return None
    try:
        uol_type = UniversalOperationalActionType(raw_action)
    except ValueError:
        return None
    try:
        surface = OperationalSurfaceType(
            str(params.get("uol_surface_type") or OperationalSurfaceType.GENERIC_SURFACE.value),
        )
    except ValueError:
        surface = OperationalSurfaceType.GENERIC_SURFACE
    ec_raw = params.get("uol_execution_contract") or {}
    eo_raw = params.get("uol_expected_outcome") or {}
    action = UniversalOperationalAction(
        uol_action=uol_type,
        surface_type=surface,
        target_entity_type=str(params.get("uol_target_entity_type") or ""),
        target_display_name=str(params.get("uol_target_display_name") or ""),
        input_text=str(params.get("uol_input_text") or ""),
        location_hint=str(params.get("uol_location_hint") or ""),
        canonical_truth=bool(params.get("uol_canonical_truth")),
        expected_outcome=OperationalOutcomeExpectation.model_validate(eo_raw)
        if isinstance(eo_raw, dict)
        else OperationalOutcomeExpectation(),
        execution_contract=OperationalExecutionContract.model_validate(ec_raw)
        if isinstance(ec_raw, dict)
        else OperationalExecutionContract(),
        source="runtime_params",
    )
    return action


# ── Human operational rendering ─────────────────────────────────────────────


def human_operational_label_for_uol(action: UniversalOperationalAction) -> str:
    """Copy humano para Mission Review (vista normal)."""
    uol = action.uol_action
    name = action.target_display_name
    query = action.input_text
    loc = action.location_hint

    if loc and action.source in ("coi_uoac", "coi") and uol not in (
        UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION,
    ):
        if not name or uol in (
            UniversalOperationalActionType.FILL_FIELD,
            UniversalOperationalActionType.OPEN_TAB,
            UniversalOperationalActionType.SEARCH_CONTENT,
        ):
            return loc

    if uol == UniversalOperationalActionType.FILL_FIELD:
        return loc or "Enfocar campo de entrada"

    if uol == UniversalOperationalActionType.SELECT_ENTITY_FROM_COLLECTION:
        if _is_profile_entity(action.target_entity_type, name) and name:
            return f"Seleccionar perfil {name}"
        if name:
            return f"Seleccionar {name}"
        return "Seleccionar elemento de la lista"

    if uol == UniversalOperationalActionType.SEARCH_CONTENT:
        if query:
            return f'Buscar "{query}"'
        return "Buscar contenido"

    if uol == UniversalOperationalActionType.OPEN_APPLICATION:
        if name:
            nice = name[0].upper() + name[1:] if name else name
            return f"Abrir {nice}"
        return "Abrir aplicación"

    if uol == UniversalOperationalActionType.OPEN_TAB:
        return "Abrir nueva pestaña"

    if uol == UniversalOperationalActionType.NAVIGATE_TO_LOCATION:
        if loc:
            # Mostrar alias legible, no URL cruda si parece dominio conocido
            if loc.startswith("http"):
                try:
                    from urllib.parse import urlparse

                    host = urlparse(loc).netloc or loc
                    site = host.replace("www.", "").split(".")[0]
                    if site:
                        return f"Abrir {site.capitalize()}"
                except Exception:
                    pass
            return f"Abrir {loc.capitalize()}"
        return "Ir a ubicación"

    if uol == UniversalOperationalActionType.SCROLL_RESULTS:
        return "Desplazar resultados"

    if uol == UniversalOperationalActionType.BROWSE_RESULTS:
        return "Explorar resultados"

    if uol == UniversalOperationalActionType.WAIT_FOR_DYNAMIC_STATE:
        return "Esperar actualización"

    if uol == UniversalOperationalActionType.FILL_FIELD:
        return "Completar campo"

    if uol == UniversalOperationalActionType.SUBMIT_INPUT:
        return "Enviar"

    return uol.value.replace("_", " ").capitalize()


def contains_technical_label_noise(label: str) -> bool:
    """True si el texto tiene vocabulario de debugger/runtime."""
    if not label:
        return False
    low = label.lower()
    return any(m in low for m in _TECHNICAL_LABEL_MARKERS)


def human_operational_label_for_review_step(
    step: Dict[str, Any],
    mission: Any = None,
) -> Optional[str]:
    """Etiqueta UOL para un paso SEP; prioriza COI del evento vinculado."""
    coi = None
    if mission is not None:
        try:
            from app.services.missions.freeze_first_execution_core import (
                find_coi_for_event,
            )

            eid = str(step.get("source_event_id") or step.get("event_id") or "")
            if eid:
                coi = find_coi_for_event(mission, eid)
        except Exception:
            coi = None
        if coi is None:
            meta_coi = (step.get("params") or {}).get("canonical_operational_intent")
            if isinstance(meta_coi, dict):
                try:
                    coi = CanonicalOperationalIntent.model_validate(meta_coi)
                except Exception:
                    coi = None

    action = compile_semantic_step_to_uol(step, coi=coi)
    if action is None:
        return None
    label = human_operational_label_for_uol(action)
    if contains_technical_label_noise(label):
        return None
    return label


# ── Ask elimination ─────────────────────────────────────────────────────────


def should_suppress_ask_for_uol(
    *,
    canonical_truth: bool = False,
    entity_confidence: float = 0.0,
    candidate_count: int = 1,
    needs_clarification: bool = False,
    ambiguity_critical: bool = False,
    expected_outcome_valid: bool = True,
) -> bool:
    """No pedir aclaración cuando la verdad operacional es suficiente."""
    if ambiguity_critical:
        return False
    if needs_clarification and not canonical_truth:
        return False
    if canonical_truth and entity_confidence >= 0.90:
        return True
    if (
        entity_confidence >= 0.90
        and candidate_count <= 1
        and expected_outcome_valid
        and not needs_clarification
    ):
        return True
    return False


def should_suppress_ask_for_mission_step(
    step: Dict[str, Any],
    mission: Any = None,
) -> bool:
    """Evalúa supresión de «Necesita aclaración» para un paso SEP."""
    params = dict(step.get("params") or {})
    coi = None
    if mission is not None:
        try:
            from app.services.missions.freeze_first_execution_core import (
                coi_from_metadata,
                mission_has_canonical_truth,
            )

            if mission_has_canonical_truth(mission):
                for ev in getattr(mission, "raw_trace", None) or []:
                    meta = dict(getattr(ev, "metadata", None) or {})
                    c = coi_from_metadata(meta)
                    if c and c.canonical_truth:
                        coi = c
                        break
        except Exception:
            pass

    if coi and coi.canonical_truth:
        conf = float(coi.truth_strength or 0.0)
        if coi.entity:
            conf = max(conf, float(coi.entity.confidence or 0.0))
        return should_suppress_ask_for_uol(
            canonical_truth=True,
            entity_confidence=conf,
            candidate_count=1,
            needs_clarification=False,
            expected_outcome_valid=True,
        )

    cand = int(params.get("candidate_count") or params.get("collection_candidate_count") or 1)
    conf = float(
        params.get("selection_confidence")
        or params.get("freeze_confidence")
        or params.get("entity_resolution_confidence")
        or 0.0,
    )
    return should_suppress_ask_for_uol(
        canonical_truth=bool(params.get("canonical_truth")),
        entity_confidence=conf,
        candidate_count=cand,
        needs_clarification=bool(params.get("needs_clarification")),
        expected_outcome_valid=bool(step.get("human_label") or params.get("query")),
    )


# ── Runtime integration ───────────────────────────────────────────────────────


def enrich_mission_step_with_uol(step: Any, mission: Any = None) -> Any:
    """Adjunta UOL a ``MissionStep.params`` antes de ejecutar."""
    from app.services.missions.execution_contracts import MissionStep

    if not isinstance(step, MissionStep):
        return step
    pseudo = {
        "type": step.kind,
        "params": dict(step.params or {}),
        "human_label": step.human_label,
    }
    coi = None
    if mission is not None:
        try:
            from app.services.missions.freeze_first_execution_core import find_coi_for_event

            eid = str((step.params or {}).get("source_event_id") or "")
            if eid:
                coi = find_coi_for_event(mission, eid)
        except Exception:
            coi = None
    action = compile_semantic_step_to_uol(pseudo, coi=coi)
    if action is None:
        return step
    uol_params = action.to_step_params()
    merged = dict(step.params or {})
    merged.update(uol_params)
    if action.execution_contract.preferred_runtime_strategies:
        pref = action.execution_contract.preferred_runtime_strategies[0]
        if action.canonical_truth and "smart_route" in pref.lower():
            pref = "select_entity:collection_first"
        merged["preferred_strategy"] = pref
        merged["fallback_strategies"] = [
            s
            for s in action.execution_contract.fallback_runtime_strategies
            if not (action.canonical_truth and "smart_route" in str(s).lower())
        ]
    if action.canonical_truth:
        merged["uol_canonical_truth"] = True
        merged["canonical_truth"] = True
    hl = human_operational_label_for_uol(action)
    if hl and not contains_technical_label_noise(hl):
        return MissionStep(
            id=step.id,
            kind=step.kind,
            params=merged,
            block_id=step.block_id,
            human_label=hl,
        )
    return MissionStep(
        id=step.id,
        kind=step.kind,
        params=merged,
        block_id=step.block_id,
        human_label=step.human_label,
    )


def resolve_uol_runtime_strategies(
    step: Any,
    contract_strategies: List[str],
) -> List[str]:
    """Orden runtime: UOL contract → ERL/UCES implícito → legacy fallback.

    No reemplaza SmartExecutor; prioriza estrategias del contrato UOL
    antes de ``smart_route`` y vocabulario legacy.
    """
    params = getattr(step, "params", None) or {}
    action = uol_from_step_params(params)
    if action is None:
        return list(contract_strategies)

    ec = action.execution_contract
    out: List[str] = []
    for s in ec.preferred_runtime_strategies + ec.fallback_runtime_strategies:
        if s and s not in out:
            out.append(s)
    for s in contract_strategies:
        if s not in out:
            low = s.lower()
            if "smart_route" in low and action.canonical_truth:
                continue
            out.append(s)
    return out


__all__ = [
    "OperationalActionGrammar",
    "OperationalExecutionContract",
    "OperationalOutcomeExpectation",
    "OperationalSurfaceType",
    "UniversalOperationalAction",
    "UniversalOperationalActionType",
    "build_universal_execution_contract",
    "compile_canonical_intent_to_uol",
    "compile_semantic_step_to_uol",
    "contains_technical_label_noise",
    "enrich_mission_step_with_uol",
    "human_operational_label_for_review_step",
    "human_operational_label_for_uol",
    "normalize_display_name",
    "resolve_uol_runtime_strategies",
    "should_suppress_ask_for_mission_step",
    "should_suppress_ask_for_uol",
    "uol_from_step_params",
]
