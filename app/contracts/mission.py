"""
ArthurOS Contracts: Mission (Super RPA)
---------------------------------------
Define el formato para grabación, compilación y reproducción de misiones semánticas RPA.

Estructura:
- RawEvent (antes Step): evento atómico crudo (mouse, teclado).
- InterpretedStep: agrupación semántica de RawEvents ("Escribir en Email").
- CompiledStep: nodo del execution graph con estrategias de resiliencia.
- Mission: objeto contenedor con trace, graph, triggers y ledger policy.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union
from datetime import datetime, timezone
from pydantic import BaseModel, Field, ConfigDict


# ============================================================================
# Enums Crudos
# ============================================================================

class EventType(str, Enum):
    MOUSE_CLICK = "mouse_click"
    MOUSE_DOUBLE_CLICK = "mouse_double_click"
    MOUSE_RIGHT_CLICK = "mouse_right_click"
    MOUSE_DRAG = "mouse_drag"
    MOUSE_SCROLL = "mouse_scroll"
    MOUSE_MOVE = "mouse_move"
    KEYBOARD_TYPE_TEXT = "keyboard_type_text"
    KEYBOARD_HOTKEY = "keyboard_hotkey"
    KEYBOARD_KEY_PRESS = "keyboard_key_press"
    WINDOW_ACTIVATE = "window_activate"
    FILE_OPERATION = "file_operation"

class AnnotationType(str, Enum):
    LABEL = "label"
    VARIABLE = "variable"
    FALLBACK = "fallback"
    VERIFICATION = "verification"
    RULE = "rule"
    NOTE = "note"

class MissionStatus(str, Enum):
    DRAFT = "draft"
    RECORDING = "recording"
    PAUSED = "paused"
    INTERPRETING = "interpreting"
    # Estado puente histórico: la misión salió del compiler pero aún no
    # ha pasado por el ApprovalGate. Se mantiene para no romper código
    # existente — los estados nuevos (``COMPILED_DRAFT`` / ``EXECUTABLE``)
    # los pone el gate.
    COMPILED = "compiled"
    # Producido por el ApprovalGate cuando hay invariantes "de sistema"
    # rotos (rutas /temp/, validation_strategy NONE, target inválido,
    # text+enter sin fusionar…) que la herramienta puede arreglar
    # automáticamente al recompilar. El usuario NO necesariamente tiene
    # que intervenir, sólo no se puede ejecutar todavía.
    COMPILED_DRAFT = "compiled_draft"
    # Producido por el ApprovalGate cuando hay un step que requiere
    # intervención humana — etiqueta de perfil vacía, click sin label,
    # cualquier ``needs_user_label=True``. El UI debe mostrar la
    # tarjeta "Quick Reinforcement" y bloquear ejecución.
    NEEDS_REVIEW = "needs_review"
    # Estado terminal positivo NUEVO: la misión cumple TODOS los
    # invariantes (ningún rojo, ninguna ruta /temp/, ningún
    # ``needs_user_label`` pendiente). Reemplaza a ``APPROVED`` para
    # toda lógica nueva — ``APPROVED`` queda como alias de
    # compatibilidad hacia atrás.
    EXECUTABLE = "executable"
    # Alias histórico de ``EXECUTABLE``. Código nuevo debe usar
    # ``EXECUTABLE``; el ``approval_gate`` mantiene ambos por compat
    # con bibliotecas existentes (skills/automations) que comprueban
    # ``status == APPROVED``.
    APPROVED = "approved"
    FAILED = "failed"

class ReplayStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    SUCCESS_WITH_RECOVERY = "success_with_recovery"  # completó tras skip/manual/recovered
    FAILED = "failed"
    CANCELLED = "cancelled"
    REQUIRES_CONFIRMATION = "requires_confirmation"


# ============================================================================
# Enums Estratégicos (Execution Graph)
# ============================================================================

class TargetResolutionStrategy(str, Enum):
    UIA_STRICT = "uia_strict"
    UIA_THEN_VISION = "uia_then_vision"
    VISION_STRICT = "vision_strict"
    OCR_SEARCH = "ocr_search"
    COORDS_ABSOLUTE = "coords_absolute"
    # Híbrido: intenta UIA → valida con imagen en región acotada → si aparece,
    # clickea el centro visual + offset; solo cae a coords absolutas si todo falla.
    ICON_VALIDATED_COORDS = "icon_validated_coords"
    # Alias semántico (producto Nevlan premium): mismo pipeline que ICON_VALIDATED_COORDS.
    COORDINATE_ICON_VALIDATED_CLICK = "coordinate_icon_validated_click"

class ActionStrategy(str, Enum):
    # Acciones primitivas (compatibles con misiones legacy).
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE_TEXT = "type_text"
    SET_FIELD_VALUE = "set_field_value"  # enfocar + escribir valor final (Field Editing Session)
    SEND_HOTKEY = "send_hotkey"
    DRAG_DROP = "drag_drop"
    SCROLL = "scroll"
    WAIT_FOR_STATE = "wait_for_state"
    EXTRACT_DATA = "extract_data"
    AGENT_PROMPT = "agent_prompt"

    # ── Acciones semánticas V3 ─────────────────────────────────
    # CLICK_TARGET es un alias semántico de CLICK con `expected_state_after`
    # explícito; se mantiene CLICK por compat. Las siguientes son nuevas:
    SCROLL_UNTIL_VISIBLE = "scroll_until_visible"   # scroll para hacer aparecer un target
    SELECT_OPTION = "select_option"                  # dropdown / select / combobox
    OPEN_MENU_ITEM = "open_menu_item"                # abrir menú + elegir opción
    CONFIRM_DIALOG = "confirm_dialog"                # aceptar/cancelar diálogo
    NAVIGATE_OR_SEARCH = "navigate_or_search"        # barra de direcciones (Ctrl+L + texto + Enter)
    SUBMIT_SEARCH = "submit_search"                  # Enter dentro de una caja de búsqueda
    LAUNCH_APP = "launch_app"                          # Windows Search: buscar + abrir app

    # ── Browser workflow primitives (PRD 2026-05-03) ───────────
    SELECT_PROFILE = "select_profile"                # Chrome/Edge profile picker
    OPEN_NEW_TAB = "open_new_tab"                    # click "Nueva pestaña" / Ctrl+T
    OPEN_BOOKMARK = "open_bookmark"                  # click bookmark → URL conocido

class ValidationStrategy(str, Enum):
    NONE = "none"
    REQUIRE_ELEMENT_EXISTS = "require_element_exists"
    REQUIRE_TEXT_MATCH = "require_text_match"
    REQUIRE_IMAGE_MATCH = "require_image_match"
    REQUIRE_DISAPPEAR = "require_disappear"
    REQUIRE_FIELD_VALUE = "require_field_value"  # post-type: UIA ValuePattern == expected

class FallbackStrategy(str, Enum):
    FAIL_FAST = "fail_fast"
    ASK_HUMAN = "ask_human"
    RETRY_WITH_VISION = "retry_with_vision"
    USE_COORDS = "use_coords"
    SKIP_STEP = "skip_step"

class TriggerType(str, Enum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    FILE_EVENT = "file_event"
    WEB_CHANGE = "web_change"
    API_WEBHOOK = "api_webhook"


# ============================================================================
# Models Crudos (Raw Trace)
# ============================================================================

class MouseAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int
    y: int
    button: str = "left"

class DragAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    duration: float = 0.5

class KeyboardAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: List[str]
    modifiers: List[str] = Field(default_factory=list)

class WindowContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hwnd: Optional[int] = None
    title: Optional[str] = None
    process_name: Optional[str] = None
    bounding_box: Optional[Dict[str, int]] = None

_RAW_EVENT_SEQ = 0


def _next_raw_event_sequence_id() -> int:
    """Genera un id monotónico de captura para reordenar eventos cuando
    los listeners (mouse/keyboard) entregan en el orden equivocado."""
    global _RAW_EVENT_SEQ
    _RAW_EVENT_SEQ += 1
    return _RAW_EVENT_SEQ


class RawEvent(BaseModel):
    """Antes Step. Evento atómico capturado."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    event_type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # sequence_id: contador monotónico asignado en el momento de captura.
    # Sirve como tie-breaker cuando dos eventos comparten ``timestamp`` (puede
    # pasar en threads de pynput a alta frecuencia). Es 0 para eventos legacy.
    sequence_id: int = Field(default_factory=_next_raw_event_sequence_id)
    offset_ms: int = 0
    delay_until_next_ms: int = 0

    mouse_action: Optional[MouseAction] = None
    drag_action: Optional[DragAction] = None
    keyboard_action: Optional[KeyboardAction] = None
    window_context: Optional[WindowContext] = None
    file_operation: Optional[Dict[str, Any]] = None

    snapshot_ref: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    annotation_type: AnnotationType
    value: str
    target_event_id: Optional[str] = None
    target_step_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    creator: str = "user"


# ============================================================================
# Models Semánticos (Execution Graph)
# ============================================================================

class RetryPolicy(BaseModel):
    """Reintentos automáticos internos por paso + límites de recuperación dirigida por usuario."""

    model_config = ConfigDict(extra="forbid")
    max_retries: int = 3  # loops automáticos antes de declarar paso fallido
    backoff_ms: int = 1000
    timeout_ms: int = 10000
    retry_allowed: bool = True  # permite «Reintentar» desde UI durante ejecución
    recovery_user_max_cycles: int = Field(
        default=10,
        ge=1,
        le=999,
        description="Máx. reintentos manuales (mismo paso) por ejecución.",
    )
    retry_reason: str = ""

class InterpretedStep(BaseModel):
    """Agrupación semántica que proponen los LLMs (ej. 'Llenar formulario')."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    description: str
    raw_event_ids: List[str] = Field(default_factory=list)
    inferred_goal: str = ""
    confidence: float = Field(
        default=1.0,
        description="0–1 legacy; usar confidence_score_pct + quality_level cuando existan.",
    )

    confidence_score_pct: Optional[float] = None
    quality_level: Optional[str] = None  # green | yellow | red


class ActionGroup(BaseModel):
    """Agrupación visual alta para revisión usuario (misión compilada)."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    group_title: str = ""
    group_summary: str = ""
    step_ids: List[str] = Field(default_factory=list)
    confidence_score: float = 0.0
    status: str = "draft"  # draft | ok | review
    quality_level: Optional[str] = None  # green | yellow | red agregado
    warnings: List[str] = Field(default_factory=list)
    expanded_default: bool = False
    semantic_tag: Optional[str] = None  # p.ej. navigate | form | save

class TargetContext(BaseModel):
    """TargetBundle V3 — contexto exhaustivo para encontrar un elemento.

    Bloques semánticos:
      - target_id      → identificador estable (hash de descriptores fuertes).
                         Permite reutilizar un mismo botón/campo en varias
                         misiones y compartir el historial de reparación.
      - semantic       → intent, human_label, role, action_kind, near_text,
                         expected_state_after. Hace explícita la INTENCIÓN
                         del usuario, no sólo cómo se ejecuta el evento.
      - desktop_uia    → AutomationId, Name, ControlType, ClassName,
                         FrameworkId, parent_chain, sibling_index, bbox,
                         clickable_point, value_pattern_supported.
      - web_data       → url, domain, frame_path, role, accessible_name,
                         label, placeholder, test_id, css, xpath,
                         dom_path, bbox + locators[] candidatos ordenados,
                         input_value_at_capture (para fields).
      - vision_data    → image_ref_small, image_ref_mid, ocr_text_around,
                         nearest_text_anchor, dominant_colors, shape_descriptor,
                         icon_hash, visual_hash, anchor_bbox.
      - coordinates    → absolute_xy, relative_to_window_fx_fy,
                         relative_to_element_dx_dy, relative_to_anchor_dx_dy.
      - confidence     → capture_confidence, last_success_score,
                         last_success_method, success_by_method (histórico
                         para autocuración / scoring), success_streak por
                         alternate, promoted_at.
      - alternates     → lista de bundles aprendidos por reparación
                         (no reemplazan, se acumulan; se promueven al
                         alcanzar 3 éxitos consecutivos).

    Todos los campos son opcionales para preservar compat con misiones viejas.
    Aceptamos ``extra="ignore"`` porque el dict puede llegar enriquecido por
    el WebRecorder/TargetResolver con metadatos adicionales benignos.

    Regla de oro: ``absolute_xy`` jamás debe ser fuente primaria del resolver
    — sólo se usa como último recurso (ver TargetResolverV2).
    """
    model_config = ConfigDict(extra="ignore")

    # ── identidad y semántica ─────────────────────────────────────
    target_id: Optional[str] = None
    # semantic: intent, human_label, role, action_kind, near_text,
    #           expected_state_after.
    semantic: Optional[Dict[str, Any]] = None

    # ── legacy / desktop UIA (compat) ────────────────────────────
    # uia_data: name, automation_id, control_type, class_name, bbox, etc.
    uia_data: Optional[Dict[str, Any]] = None
    image_ref: Optional[str] = None               # snapshot small (~100px)
    image_ref_mid: Optional[str] = None           # snapshot mid (~600px)
    ocr_text_expected: Optional[str] = None
    fallback_coords: Optional[Dict[str, int]] = None
    anchor_bbox: Optional[Dict[str, int]] = None
    click_offset_within_bbox: Optional[Dict[str, int]] = None
    fallback_coords_relative_to_window: Optional[Dict[str, float]] = None
    process_name: Optional[str] = None
    window_title: Optional[str] = None
    hwnd: Optional[int] = None

    # ── TargetBundle V3 (rich) ────────────────────────────────────
    # desktop_uia: ver Phase 1 del PRD (parent_chain, sibling_index,
    #              clickable_point, value_pattern_supported, runtime_id).
    desktop_uia: Optional[Dict[str, Any]] = None
    # web_data: ver app/services/missions/web_recorder.py — incluye `locators`
    #            (lista de candidatos ordenados por especificidad) y
    #            `input_value_at_capture` para fields.
    web_data: Optional[Dict[str, Any]] = None
    # vision_data: ver app/services/missions/target_resolver.py — incluye
    #              ocr_text_around, nearest_text_anchor, visual_hash,
    #              dominant_colors, icon_hash.
    vision_data: Optional[Dict[str, Any]] = None
    # confidence: capture_score (0-100), last_success_score (0-100),
    #             last_success_method (web_locator/uia_exact/...),
    #             success_by_method: {method: {ok: int, fail: int}},
    #             alternate_streaks: {alternate_idx: int}.
    confidence: Optional[Dict[str, Any]] = None
    # alternates: lista de TargetBundle alternativos aprendidos por
    #             reparación. Cada elemento es un dict serializable
    #             con la misma forma que TargetContext (subset relevante).
    #             Se promueven al primary cuando acumulan 3 éxitos seguidos.
    alternates: List[Dict[str, Any]] = Field(default_factory=list)
    signature: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Firma Nevlan enriquecida (TargetSignature blob).",
    )

class CompiledStep(BaseModel):
    """Nodo del Grafo de Ejecución."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    goal: str = "Execute Action"
    
    target_context: TargetContext = Field(default_factory=TargetContext)
    
    target_resolution_strategy: TargetResolutionStrategy = TargetResolutionStrategy.UIA_THEN_VISION
    action_strategy: ActionStrategy = ActionStrategy.CLICK
    validation_strategy: ValidationStrategy = ValidationStrategy.NONE
    fallback_strategy: FallbackStrategy = FallbackStrategy.RETRY_WITH_VISION
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    
    # Payloads dependientes de la acción
    action_payload: Dict[str, Any] = Field(default_factory=dict) # ej. text_to_type, double_click args
    
    # Control de flujo
    next_step_id_on_success: Optional[str] = None
    next_step_id_on_failure: Optional[str] = None


# ============================================================================
# Motor de Triggers y Políticas
# ============================================================================

class TriggerDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    type: TriggerType
    config: Dict[str, Any] = Field(default_factory=dict) # e.g. cron_expression, file_path, webhook_url
    is_active: bool = True

class MissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ledger_enabled: bool = True
    auto_recoverable: bool = True
    notifications_enabled: bool = False
    requires_human_approval_on_fail: bool = True


# ============================================================================
# Audit trail de Mission Review (PRD 2026-05-08 §H)
# ============================================================================
#
# Cuando el ICE no logra extraer un dato directamente del raw_trace
# (perfil ambiguo, query con conector faltante…) y el usuario lo
# confirma desde Mission Review, registramos esa confirmación como
# entrada estructurada en ``Mission.review_confirmations``.
#
# Razones de diseño:
#
#   * El JSON de la misión NO debe ser engañoso: si la grabación
#     vino incompleta y el usuario tuvo que reforzar, debe quedar
#     evidencia explícita en el archivo persistido.
#   * El ``raw_trace`` SIEMPRE permanece intacto — no se reescribe
#     bajo ninguna circunstancia (auditoría forense).
#   * Cada entrada apunta al ``step_id`` del plan semántico al que
#     se aplicó, para poder reconstruir la cadena de decisiones.
#   * ``source`` es un enum textual: ``"user_confirmation"`` (el
#     usuario tipeó/aceptó), ``"alias_store"`` (lo aprendimos de
#     una sesión anterior y se aplicó automáticamente), o
#     ``"heuristic_suggestion_accepted"`` (el usuario aceptó la
#     sugerencia que el ICE preseleccionó).

class ReviewConfirmation(BaseModel):
    """Una confirmación humana sobre un dato del plan semántico."""
    model_config = ConfigDict(extra="ignore")

    field: str  # ej. "profile_name", "query"
    value: str  # el valor confirmado
    source: str = "user_confirmation"
    step_id: Optional[str] = None
    step_type: Optional[str] = None
    original_value: Optional[str] = None  # lo que el ICE había puesto
    confirmed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    note: str = ""  # contexto opcional (ej. heurística disparada)


# ============================================================================
# Audit trail de Regrabación de paso (PRD 2026-05-08c §E + §F)
# ============================================================================
#
# Cuando el usuario regraba un paso N de la misión, persistimos una
# entrada estructurada en ``mission.rerecord_history`` describiendo:
#
#   * qué paso original fue reemplazado;
#   * cuál es el ID del paso nuevo;
#   * qué pasos previos se reprodujeron (para llegar al contexto);
#   * qué pasos posteriores se vieron afectados (por dependencias
#     declaradas, no implícitas);
#   * timestamps (started_at, saved_at);
#   * resultado final (``saved`` | ``cancelled``).
#
# Esta colección **NO** sustituye a ``review_confirmations`` —
# ``ReviewConfirmation`` registra a nivel de campo (``"profile_name"``,
# ``"query"``, ``"step_replacement"``); ``RerecordHistoryEntry`` registra
# a nivel de sesión completa (con replay previo y audit del trayecto).
#
# El ``raw_trace`` original se conserva intacto. La nueva captura del
# paso N se guarda dentro de la entrada como ``replacement_evidence``
# para preservar trazabilidad forense ("¿qué eventos crudos llevaron
# a este paso reemplazado?").

class SemanticHypothesisStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class SemanticHypothesis(BaseModel):
    """Hipótesis semántica emitida en grabación (modo shadow).

    No sustituye al SEP/SIPE; sirve para auditoría y comparación offline.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    event_id: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_event_ids: List[str] = Field(default_factory=list)
    candidate_intent_type: str = ""
    candidate_capability_id: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence_label: str = ""
    evidence_refs: List[str] = Field(default_factory=list)
    carriers: List[str] = Field(default_factory=list)
    alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    ambiguity: Dict[str, Any] = Field(default_factory=dict)
    execution_truth_snapshot: Dict[str, Any] = Field(default_factory=dict)
    semantic_ambiguity_snapshot: Dict[str, Any] = Field(default_factory=dict)
    status: SemanticHypothesisStatus = SemanticHypothesisStatus.PROPOSED
    created_by: str = "recorder_semantic_shadow"
    notes: str = ""


class IntentSnapshot(BaseModel):
    """Instantánea ligera del estado de intención inferida durante la grabación."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hypothesis_ids: List[str] = Field(default_factory=list)
    dominant_candidate_types: List[str] = Field(default_factory=list)
    summary: str = ""


class SemanticSessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    AMBIGUOUS = "ambiguous"


class OperationalIntentStatus(str, Enum):
    PROPOSED = "proposed"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class SemanticSession(BaseModel):
    """Sesión operacional continua (Semantic Session Engine — observación / sombra)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    session_type: str = ""  # launcher_session | navigation_session | ...
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    status: SemanticSessionStatus = SemanticSessionStatus.ACTIVE

    source_event_ids: List[str] = Field(default_factory=list)
    hypothesis_ids: List[str] = Field(default_factory=list)
    dominant_intent: str = ""
    confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    context: Dict[str, Any] = Field(default_factory=dict)
    inferred_goal: str = ""
    related_targets: List[Dict[str, Any]] = Field(default_factory=list)

    execution_truth_snapshot: Dict[str, Any] = Field(default_factory=dict)
    semantic_ambiguity_snapshot: Dict[str, Any] = Field(default_factory=dict)

    absorbed_events_count: int = 0
    absorbed_hypotheses_count: int = 0
    transitions: List[Dict[str, Any]] = Field(default_factory=list)
    completion_signals: List[str] = Field(default_factory=list)
    interruption_reason: Optional[str] = None
    notes: str = ""


class OperationalIntent(BaseModel):
    """Intención operacional derivada de una sesión (no eventos atómicos)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    session_id: str = ""
    intent_type: str = ""  # navigate_to_site | submit_search_query | ...
    capability_id: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    evidence: List[str] = Field(default_factory=list)
    outcome: Optional[str] = None
    status: OperationalIntentStatus = OperationalIntentStatus.PROPOSED


class CanonicalOperationalBlockStatus(str, Enum):
    """Estado de promoción de un COB (capa sombra / híbrido)."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class CanonicalOperationalBlock(BaseModel):
    """Bloque operacional canónico — intención ejecutable portable (Fase 1 shadow).

    No sustituye SEP ni replay; convive para auditoría y comparación.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    session_id: str = ""
    operational_intent_id: str = ""
    block_type: str = ""  # launcher_session | navigation_session | ...
    capability_id: Optional[str] = None
    canonical_action: str = ""  # open_application | navigate_to_site | ...
    params: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    ambiguity: Dict[str, Any] = Field(default_factory=dict)
    execution_truth_snapshot: Dict[str, Any] = Field(default_factory=dict)
    semantic_ambiguity_snapshot: Dict[str, Any] = Field(default_factory=dict)
    source_event_ids: List[str] = Field(default_factory=list)
    source_hypothesis_ids: List[str] = Field(default_factory=list)
    source_session_ids: List[str] = Field(default_factory=list)
    absorbed_steps: int = Field(
        default=0,
        ge=0,
        description="Nº de eventos crudos absorbidos por la sesión fuente.",
    )
    expected_outcomes: List[str] = Field(default_factory=list)
    completion_signals: List[str] = Field(default_factory=list)
    replay_strategy: Dict[str, Any] = Field(default_factory=dict)
    repair_strategy: Dict[str, Any] = Field(default_factory=dict)
    stability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    portability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    execution_priority: int = Field(default=50, ge=0, le=100)
    created_by: str = "canonical_operational_blocks"
    status: CanonicalOperationalBlockStatus = CanonicalOperationalBlockStatus.PROPOSED
    notes: str = ""


class CobReplayPlanStep(BaseModel):
    """Un paso del replay propuesto por COB (solo sombra — no ejecuta input real)."""

    model_config = ConfigDict(extra="ignore")

    order: int = Field(ge=1)
    phase_id: str = ""
    label: str = ""
    primitive: str = ""
    params_keys: List[str] = Field(default_factory=list)
    validation_hints: List[str] = Field(default_factory=list)


class CobSingleReplayPlan(BaseModel):
    """ReplayPlan simulado para un único COB."""

    model_config = ConfigDict(extra="ignore")

    cob_id: str = ""
    canonical_action: str = ""
    session_id: str = ""
    steps: List[CobReplayPlanStep] = Field(default_factory=list)
    operational_readiness: float = Field(default=0.0, ge=0.0, le=1.0)
    gaps: List[str] = Field(default_factory=list)


# ── Truth Extraction Layer (TEL), Fase 1 — modo sombra ───────────────────────
#
# Fuente operacional humano-limpia paralela al SEP / COB. No sustituye
# ejecución; persiste auditoría + comparativa vs SEP.


class OperationalTruthStatus(str, Enum):
    """Estado de emisión del bloque (sombra hasta integración SEP)."""

    COMMITTED_SHADOW = "committed_shadow"
    COMMITTED_PARTIAL_BOUNDARY = "committed_partial_boundary"
    WITHHELD_UNSTABLE = "withheld_unstable"


class OperationalTruthBlock(BaseModel):
    """Bloque único de verdad operacional humana emitido por TEL.

    Separación absoluta intent operacional vs mecánica de runtime en la
    capa superior; los snapshots conservan aislamiento target/outcome sin
    redefiniciones cruzadas.
    """

    model_config = ConfigDict(extra="ignore")

    truth_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))

    truth_type: str = Field(
        default="",
        description=(
            "Tipo humano-operacional e.g. open_application, navigate_to_location;"
            " jamás primitives UIA/DOM/smart_route como tipo."
        ),
    )
    canonical_action: str = Field(
        default="",
        description="Etiqueta operacional estable (usualmente igual a truth_type).",
    )
    human_intent: str = Field(
        default="",
        description="Descripción corta pensada para revisión humana / producto.",
    )

    absorbed_events: List[str] = Field(default_factory=list)
    absorbed_hypotheses: List[str] = Field(default_factory=list)
    absorbed_sessions: List[str] = Field(default_factory=list)

    context_window: Dict[str, Any] = Field(default_factory=dict)
    stability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    operational_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity_score: float = Field(default=0.5, ge=0.0, le=1.0)
    execution_readiness: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_human_confirmation: bool = Field(default=False)

    target_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description="Qué el humano apuntaba (identity isolation, sin outcome).",
    )
    outcome_snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description="Signal post-acción / contexto estable; jamás redefine target.",
    )

    temporal_signature: Dict[str, Any] = Field(default_factory=dict)
    replay_anchor_candidates: List[str] = Field(default_factory=list)
    validator_expectations: List[str] = Field(default_factory=list)

    emitted_from: str = Field(default="truth_extraction_layer")
    truth_status: OperationalTruthStatus = OperationalTruthStatus.COMMITTED_SHADOW

    debug_trace: Dict[str, Any] = Field(default_factory=dict)


# ── Operational Truth Preservation (OTP) — cadena extremo a extremo ─────────
#
# Rastrea verdad operacional canónica desde grabación hasta Mission Review /
# runtime. Cada transformación registra entrada/salida y pérdida exacta.


class OperationalTruthStage(str, Enum):
    """Etapa del pipeline donde se observó o transformó la verdad."""

    RECORDER = "recorder"
    FFEC = "ffec"
    COI = "coi"
    ICE = "ice"
    UCES = "uces"
    SEP = "sep"
    UOL_COMPILER = "uol_compiler"
    MISSION_REVIEW = "mission_review"
    RUNTIME_PLANNER = "runtime_planner"


class OperationalTruthDisposition(str, Enum):
    """Resultado de una transformación sobre un nodo de verdad."""

    PRESERVED = "preserved"
    DEGRADED = "degraded"
    LOST = "lost"
    TRANSFORMED = "transformed"


class OperationalTruthNode(BaseModel):
    """Una verdad operacional en un punto concreto del pipeline."""

    model_config = ConfigDict(extra="ignore")

    node_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    truth_origin_step_id: str = Field(
        default="",
        description="Id estable de la verdad a través de etapas (COI intent_id, SEP step id, …).",
    )
    stage: OperationalTruthStage = OperationalTruthStage.RECORDER
    disposition: OperationalTruthDisposition = OperationalTruthDisposition.PRESERVED
    truth_preserved: bool = True
    truth_loss_reason: str = ""
    human_label: str = ""
    operational_kind: str = ""
    canonical_truth: bool = False
    coi_intent_id: str = ""
    source_event_id: str = ""
    parent_node_id: str = ""
    received_from_stage: Optional[OperationalTruthStage] = None
    emitted_to_stage: Optional[OperationalTruthStage] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SurfaceTransitionClassification(str, Enum):
    """Tipo de cambio de superficie observado tras una acción canónica."""

    NONE = "none"
    PROCESS_CHANGE = "process_change"
    WINDOW_SHELL_CHANGE = "window_shell_change"
    VIEWPORT_CHANGE = "viewport_change"
    APPLICATION_HANDOFF = "application_handoff"
    CONTENT_SURFACE = "content_surface"


class TruthContinuityState(str, Enum):
    """Estado de continuidad del hilo operacional."""

    PRESERVED = "preserved"
    DEGRADED = "degraded"
    BROKEN = "broken"
    UNKNOWN = "unknown"


class OperationalTransitionBridge(BaseModel):
    """Puente universal entre superficies (sin hardcode de producto)."""

    model_config = ConfigDict(extra="ignore")

    bridge_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    bridge_type: str = ""
    from_surface: str = ""
    to_surface: str = ""
    from_process: str = ""
    to_process: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_event_id: str = ""
    coi_intent_id: str = ""
    thread_id: str = ""
    surface_transition_type: SurfaceTransitionClassification = (
        SurfaceTransitionClassification.NONE
    )


class OperationalThread(BaseModel):
    """Hilo operacional estable a través de cambios de hwnd/proceso/DOM."""

    model_config = ConfigDict(extra="ignore")

    thread_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    truth_origin_step_id: str = ""
    coi_intent_id: str = ""
    source_event_id: str = ""
    human_label: str = ""
    operational_kind: str = "select_entity_from_collection"
    canonical_truth: bool = False
    truth_preserved: bool = False
    continuity_preserved: bool = False
    continuity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    entity_display_name: str = ""
    entity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    transition_expected: bool = False
    transition_observed: bool = False
    continuity_bridge_used: str = ""
    surface_transition_type: SurfaceTransitionClassification = (
        SurfaceTransitionClassification.NONE
    )
    truth_continuity_state: TruthContinuityState = TruthContinuityState.UNKNOWN
    operational_element_type: str = ""
    element_identity_id: str = ""


class OperationalContinuityChain(BaseModel):
    """Cadena OCRX: continuidad operacional extremo a extremo."""

    model_config = ConfigDict(extra="ignore")

    chain_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    threads: List[OperationalThread] = Field(default_factory=list)
    bridges: List[OperationalTransitionBridge] = Field(default_factory=list)
    continuity_preserved: bool = False
    continuity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    truth_preserved: bool = False
    last_audit_at: str = ""


class OperationalTruthChain(BaseModel):
    """Cadena auditable de verdad operacional para una misión."""

    model_config = ConfigDict(extra="ignore")

    chain_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    nodes: List[OperationalTruthNode] = Field(default_factory=list)
    preserved: List[str] = Field(
        default_factory=list,
        description="truth_origin_step_id con disposición preserved en etapa final.",
    )
    degraded: List[str] = Field(default_factory=list)
    lost: List[str] = Field(default_factory=list)
    transformed: List[str] = Field(default_factory=list)
    loss_by_stage: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="truth_origin_step_id → etapa donde murió la verdad (ICE, UCES, …).",
    )
    last_audit_at: str = ""


class RerecordHistoryEntry(BaseModel):
    """Una sesión completa de regrabación de un paso.

    Registrada en ``Mission.rerecord_history`` cada vez que el usuario
    cierra una sesión de regrabación con ``status="saved"``. Las
    sesiones canceladas NO se persisten aquí (ver `cancel()` del
    orchestrator) — solo aparecen en el log técnico.
    """
    model_config = ConfigDict(extra="ignore")

    rerecord_session_id: str
    target_step_id: str
    target_step_index: int
    original_step_snapshot: Dict[str, Any] = Field(default_factory=dict)
    replacement_step_snapshot: Dict[str, Any] = Field(default_factory=dict)
    replacement_evidence: List[Dict[str, Any]] = Field(default_factory=list)
    previous_steps_replayed: List[str] = Field(default_factory=list)
    affected_steps: List[str] = Field(default_factory=list)
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    saved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    source: str = "user_rerecord"
    status: str = "saved"  # "saved" | "cancelled" | "failed"
    note: str = ""


# ── Live Operational Perception (LOP), Fase 1 sombra ─────────────────────────
# Percepción operacional viva: fusiona UIA/DOM/OCR ligero/validators/contexto.
# No ejecuta acciones; GOOR/OCR/ARL consumen snapshots como eco declarativo.


class LiveOperationalSurfaceType(str, Enum):
    LAUNCHER_SURFACE = "launcher_surface"
    BROWSER_SURFACE = "browser_surface"
    SEARCH_SURFACE = "search_surface"
    RESULTS_SURFACE = "results_surface"
    FORM_SURFACE = "form_surface"
    DIALOG_SURFACE = "dialog_surface"
    FILE_SURFACE = "file_surface"
    TABLE_SURFACE = "table_surface"
    DASHBOARD_SURFACE = "dashboard_surface"
    EDITOR_SURFACE = "editor_surface"
    UNKNOWN_SURFACE = "unknown_surface"


class LiveOperationalEntityKind(str, Enum):
    BUTTON = "button"
    LINK = "link"
    INPUT = "input"
    SEARCHBOX = "searchbox"
    MENU_ITEM = "menu_item"
    TAB = "tab"
    PROFILE_OPTION = "profile_option"
    FILE = "file"
    FOLDER = "folder"
    TABLE = "table"
    ROW = "row"
    CARD = "card"
    DIALOG = "dialog"
    ALERT = "alert"
    RESULT_ITEM = "result_item"


class LiveOperationalRuntimeAction(str, Enum):
    CONTINUE = "CONTINUE"
    WAIT = "WAIT"
    REVALIDATE = "REVALIDATE"
    RECOVER = "RECOVER"
    REQUEST_HUMAN = "REQUEST_HUMAN"
    STOP_UNSAFE = "STOP_UNSAFE"


class LiveOperationalSignal(BaseModel):
    """Señal atómica de una fuente de percepción."""

    model_config = ConfigDict(extra="ignore")

    kind: str = Field(
        default="observation",
        description="p.ej. uia_role, dom_anchor, validator, tel_expectation",
    )
    source: str = Field(
        default="uia",
        description="uia | dom | ocr_light | visual_anchor | runtime_validator | operational_context",
    )
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    payload: Dict[str, Any] = Field(default_factory=dict)


class LiveOperationalEntity(BaseModel):
    """Entidad operacional observable (agnóstica de producto)."""

    model_config = ConfigDict(extra="ignore")

    entity_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    kind: LiveOperationalEntityKind = LiveOperationalEntityKind.INPUT
    label_guess: str = ""
    roles: List[str] = Field(default_factory=list)
    automation_ids: List[str] = Field(default_factory=list)
    text_snippets: List[str] = Field(default_factory=list)
    dom_hints: Dict[str, Any] = Field(default_factory=dict)
    uia_hints: Dict[str, Any] = Field(default_factory=dict)
    actionable: bool = True
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class LiveOperationalSurface(BaseModel):
    """Superficie operacional inferida con evidencia."""

    model_config = ConfigDict(extra="ignore")

    surface_type: LiveOperationalSurfaceType = LiveOperationalSurfaceType.UNKNOWN_SURFACE
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: List[str] = Field(default_factory=list)


class LiveOperationalBlocker(BaseModel):
    model_config = ConfigDict(extra="ignore")

    blocker_type: str = Field(
        default="unknown",
        description=(
            "modal_blocking | auth_required | permission_dialog | multiple_equivalent_targets | "
            "unknown_destructive_confirmation | network_loading_stall | unexpected_app_switch | "
            "missing_required_surface | validator_mismatch | low_confidence | other"
        ),
    )
    severity: str = Field(default="medium", description="low | medium | high | critical")
    description: str = ""
    evidence_signals: List[str] = Field(default_factory=list)


class LiveOperationalValidatorObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    validator_key: str = ""
    satisfied: bool = False
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    detail: str = ""


class LiveOperationalTopology(BaseModel):
    """Topología relativa liviana (sin pixels ni CLIP)."""

    model_config = ConfigDict(extra="ignore")

    uia_estimated_depth: int = Field(default=0, ge=0)
    dom_estimated_depth: int = Field(default=0, ge=0)
    scroll_container_count: int = Field(default=0, ge=0)
    viewport_regions_hint: List[str] = Field(default_factory=list)
    fingerprint_refs: List[str] = Field(default_factory=list)


class LiveOperationalPerceptionSnapshot(BaseModel):
    """Instantánea LOP — estado operacional vivo inferido."""

    model_config = ConfigDict(extra="ignore")

    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    active_app: str = ""
    active_window: str = ""
    active_url: str = ""
    active_domain: str = ""
    surface_type: LiveOperationalSurfaceType = LiveOperationalSurfaceType.UNKNOWN_SURFACE
    operational_state_guess: str = ""
    matched_org_node_ids: List[str] = Field(default_factory=list)
    matched_goal_ids: List[str] = Field(default_factory=list)
    visible_entities: List[LiveOperationalEntity] = Field(default_factory=list)
    actionable_entities: List[LiveOperationalEntity] = Field(default_factory=list)
    input_surfaces: List[LiveOperationalSurface] = Field(default_factory=list)
    navigation_surfaces: List[LiveOperationalSurface] = Field(default_factory=list)
    result_surfaces: List[LiveOperationalSurface] = Field(default_factory=list)
    blocking_surfaces: List[LiveOperationalSurface] = Field(default_factory=list)
    live_blockers: List[LiveOperationalBlocker] = Field(
        default_factory=list,
        description="Bloqueos operacionales vivos detectados (no ejecutables).",
    )
    modal_detected: bool = False
    validators_observed: List[LiveOperationalValidatorObservation] = Field(default_factory=list)
    execution_truth_signals: List[LiveOperationalSignal] = Field(default_factory=list)
    continuity_signals: List[LiveOperationalSignal] = Field(default_factory=list)
    ambiguity_signals: List[LiveOperationalSignal] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_reasons: List[str] = Field(default_factory=list)
    perception_gaps: List[str] = Field(default_factory=list)
    recommended_runtime_action: LiveOperationalRuntimeAction = LiveOperationalRuntimeAction.WAIT
    safe_to_continue: bool = False
    topology: LiveOperationalTopology = Field(default_factory=LiveOperationalTopology)
    raw_sensor_stats: Dict[str, Any] = Field(
        default_factory=dict,
        description="Conteos/agregados sin PII; p.ej. ocr_invoked, elapsed_ms.",
    )


class LopSensorHealthStatus(str, Enum):
    """Salud del stack de sensores LOP Sensor Bridge."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class LopSensorBudget(BaseModel):
    """Presupuestos de tiempo/recolección para LOP Sensor Bridge (read-only)."""

    model_config = ConfigDict(extra="ignore")

    total_timeout_ms: int = Field(default=450, ge=5, le=120_000)
    uia_timeout_ms: int = Field(default=120, ge=5, le=60_000)
    dom_timeout_ms: int = Field(default=120, ge=5, le=60_000)
    ocr_timeout_ms: int = Field(default=90, ge=5, le=60_000)
    max_uia_nodes: int = Field(default=180, ge=4, le=5000)
    max_uia_depth: int = Field(default=8, ge=1, le=48)
    max_dom_nodes: int = Field(default=120, ge=4, le=5000)
    max_ocr_regions: int = Field(default=2, ge=0, le=32)
    max_ocr_pixels: int = Field(default=400_000, ge=1024, le=50_000_000)
    max_snapshot_bytes: int = Field(default=512_000, ge=4096, le=50_000_000)
    text_sufficiency_chars: int = Field(default=48, ge=0, le=20_000)


class LopSensorHealth(BaseModel):
    """Diagnóstico agregado de sensores (sin PII de pantalla)."""

    model_config = ConfigDict(extra="ignore")

    status: LopSensorHealthStatus = LopSensorHealthStatus.HEALTHY
    reasons: List[str] = Field(default_factory=list)


class LopSensorAudit(BaseModel):
    """Auditoría estructural LOP Sensor Bridge (serializable JSON)."""

    model_config = ConfigDict(extra="ignore")

    sensors_used: List[str] = Field(default_factory=list)
    sensors_failed: List[str] = Field(default_factory=list)
    budget_used_ms: float = Field(default=0.0, ge=0.0)
    nodes_collected: int = Field(default=0, ge=0)
    dom_nodes_collected: int = Field(default=0, ge=0)
    ocr_regions_used: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    blockers_detected: int = Field(default=0, ge=0)
    perception_gaps: List[str] = Field(default_factory=list)
    health_status: LopSensorHealthStatus = LopSensorHealthStatus.HEALTHY
    snapshot_bytes_estimate: int = Field(default=0, ge=0)
    runner_shadow_steps: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Muestras before/after acotadas (SmartRunner shadow).",
    )


class LopSensorCaptureResult(BaseModel):
    """Metadatos de una captura LOP desde sensores reales/mock."""

    model_config = ConfigDict(extra="ignore")

    ok: bool = True
    budget_exceeded_flags: List[str] = Field(default_factory=list)
    partial_safe: bool = True
    health: LopSensorHealth = Field(default_factory=LopSensorHealth)
    started_at_perf: float = 0.0
    finished_at_perf: float = 0.0


class LopSensorBridgeConfig(BaseModel):
    """Config observable del bridge — no ejecuta acciones."""

    model_config = ConfigDict(extra="ignore")

    budget: LopSensorBudget = Field(default_factory=LopSensorBudget)
    include_uia: bool = True
    include_dom: bool = True
    include_visual_anchor_hints: bool = True
    allow_ocr_on_demand: bool = True


# ── Pre-Click Operational Freeze (PCOF) ─────────────────────────────────────
# Congela identidad operacional viva ANTES de mouse_up / transición de UI.
# post_action_state solo valida outcome; nunca identifica el target original.


class TransitionRiskLevel(str, Enum):
  LOW = "low"
  MEDIUM = "medium"
  HIGH = "high"


class OperationalFreezeTarget(BaseModel):
    """Target operacional congelado (UIA / OCR / DOM)."""

    model_config = ConfigDict(extra="ignore")

    source: str = ""
    role: str = ""
    name: str = ""
    automation_id: str = ""
    bbox: Dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    parent_chain: List[str] = Field(default_factory=list)
    dom_selector: str = ""
    overlap_score: float = Field(default=0.0, ge=0.0, le=1.0)


class OperationalFreezeEntityCandidate(BaseModel):
    """Candidato a entidad operacional fusionado pre-transición."""

    model_config = ConfigDict(extra="ignore")

    display_name: str = ""
    normalized_name: str = ""
    entity_type_hint: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_sources: List[str] = Field(default_factory=list)
    uia_name: str = ""
    ocr_text: str = ""
    dom_text: str = ""
    bbox: Dict[str, Any] = Field(default_factory=dict)
    collection_role: str = ""
    absorbed_ambiguity: bool = False


class OperationalFreezeCollectionSnapshot(BaseModel):
    """Colección operacional visible congelada antes del click."""

    model_config = ConfigDict(extra="ignore")

    collection_type: str = ""
    display_name: str = ""
    entities: List[str] = Field(default_factory=list)
    entity_count: int = 0
    structural_hash: str = ""
    scrollable: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class OperationalFreezeSnapshot(BaseModel):
    """Estado operacional congelado antes de ejecutar o registrar el click."""

    model_config = ConfigDict(extra="ignore")

    freeze_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    captured_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    cursor_xy: Tuple[int, int] = (0, 0)
    window_context: Dict[str, Any] = Field(default_factory=dict)
    viewport_signature: str = ""

    uia_targets: List[OperationalFreezeTarget] = Field(default_factory=list)
    ocr_targets: List[OperationalFreezeTarget] = Field(default_factory=list)
    dom_targets: List[OperationalFreezeTarget] = Field(default_factory=list)

    operational_collections: List[OperationalFreezeCollectionSnapshot] = Field(
        default_factory=list,
    )
    collection_entities: List[OperationalFreezeEntityCandidate] = Field(
        default_factory=list,
    )

    best_entity_candidate: Optional[OperationalFreezeEntityCandidate] = None
    best_collection_candidate: Optional[OperationalFreezeCollectionSnapshot] = None

    nearby_labels: List[str] = Field(default_factory=list)
    visual_group_signature: str = ""
    layout_signature: str = ""

    freeze_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    freeze_quality: str = ""
    freeze_reasons: List[str] = Field(default_factory=list)

    transition_risk: TransitionRiskLevel = TransitionRiskLevel.LOW
    dynamic_surface_detected: bool = False

    pre_transition_confirmed: bool = False
    trigger: str = ""
    freeze_depth: str = "standard"
    hierarchy_chain: List[str] = Field(default_factory=list)
    active_affordances: List[str] = Field(default_factory=list)


# ── Freeze-First Execution Core (FFEC) — verdad canónica pre-transición ─────
# Cuando el freeze confirma identidad antes del cambio de UI, esa verdad es
# inmutable: after_state solo valida outcome, nunca redefine el target.


class CoiTruthOrigin(str, Enum):
    """Procedencia de la verdad canónica."""

    PRE_CLICK_OPERATIONAL_FREEZE = "pre_click_operational_freeze"
    NONE = "none"


class CoiResolutionPolicy(str, Enum):
    """Cómo resolver el target en replay."""

    FREEZE_ENTITY_COLLECTION = "freeze_entity_collection"
    ENTITY_THEN_COLLECTION = "entity_then_collection"
    LEGACY_FALLBACK = "legacy_fallback"


class CoiExecutionPolicy(str, Enum):
    """Prioridad de ejecución cuando existe COI."""

    COI_UCES_ERL_LONE_UIA_OCR_VISION_COORDS_EMERGENCY = (
        "coi_uces_erl_lone_uia_ocr_vision_coords_emergency"
    )


class CoiValidationPolicy(str, Enum):
    """Qué valida el éxito de la acción."""

    INTENDED_ENTITY_AND_EXPECTED_TRANSITION = (
        "intended_entity_and_expected_transition"
    )
    AFTER_STATE_OUTCOME_ONLY = "after_state_outcome_only"


class OperationalAffordanceType(str, Enum):
    """Función operacional universal de un elemento (UOAC), no su etiqueta UIA."""

    ACTIVATE_LAUNCHER = "activate_launcher"
    CREATE_NEW_CONTEXT = "create_new_context"
    NAVIGATE_TO_RESOURCE = "navigate_to_resource"
    SELECT_IDENTITY_CONTEXT = "select_identity_context"
    FOCUS_INPUT = "focus_input"
    SEARCH_WITH_QUERY = "search_with_query"
    SUBMIT_INPUT = "submit_input"
    BROWSE_CONTENT = "browse_content"
    OPEN_MENU = "open_menu"
    CHOOSE_OPTION = "choose_option"
    CONFIRM_ACTION = "confirm_action"
    DISMISS_DIALOG = "dismiss_dialog"
    UNKNOWN = "unknown"


class OperationalAffordance(BaseModel):
    """Clasificación UOAC: qué función operacional tenía el elemento tocado."""

    model_config = ConfigDict(extra="ignore")

    affordance_type: OperationalAffordanceType = OperationalAffordanceType.UNKNOWN
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_sources: List[str] = Field(default_factory=list)
    operational_intent_kind: str = ""
    uol_action_hint: str = ""
    human_label_hint: str = ""
    should_materialize_as_step: bool = True
    should_merge_with_adjacent_events: bool = False
    transition_expectation: str = ""
    ambiguity_reason: str = ""


class OperationalElementType(str, Enum):
    """Tipo universal de elemento operacional (UOEI) — qué ES el elemento."""

    # INPUT / QUERY
    LAUNCHER_INPUT_SURFACE = "launcher_input_surface"
    SEARCH_INPUT_SURFACE = "search_input_surface"
    FORM_INPUT_SURFACE = "form_input_surface"

    # NAVIGATION
    NAVIGATION_RESOURCE = "navigation_resource"
    NAVIGATION_AFFORDANCE = "navigation_affordance"
    BROWSER_NAVIGATION_SURFACE = "browser_navigation_surface"

    # CONTEXT
    CONTEXT_CREATION_AFFORDANCE = "context_creation_affordance"
    CONTEXT_SWITCH_AFFORDANCE = "context_switch_affordance"
    SESSION_SWITCH_SURFACE = "session_switch_surface"

    # IDENTITY
    IDENTITY_CARD = "identity_card"
    SELECTABLE_IDENTITY = "selectable_identity"
    PROFILE_SURFACE = "profile_surface"

    # CONTENT
    CONTENT_SURFACE = "content_surface"
    RESULTS_CONTAINER = "results_container"
    SCROLLABLE_RESULTS_SURFACE = "scrollable_results_surface"

    # ACTION
    CONFIRMATION_AFFORDANCE = "confirmation_affordance"
    SUBMIT_AFFORDANCE = "submit_affordance"
    DESTRUCTIVE_AFFORDANCE = "destructive_affordance"

    # STRUCTURAL
    COLLECTION_CONTAINER = "collection_container"
    OPERATIONAL_WORKSPACE = "operational_workspace"
    MODAL_SURFACE = "modal_surface"

    # UNKNOWN
    UNKNOWN_OPERATIONAL_ELEMENT = "unknown_operational_element"


class OperationalElementIdentity(BaseModel):
    """Identidad operacional universal (UOEI) — multi-señal, estable entre superficies."""

    model_config = ConfigDict(extra="ignore")

    identity_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    operational_element_type: OperationalElementType = (
        OperationalElementType.UNKNOWN_OPERATIONAL_ELEMENT
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    structural_signature: str = ""
    visual_signature: str = ""
    behavioral_signature: str = ""
    contextual_signature: str = ""
    interaction_signature: str = ""
    continuity_signature: str = ""
    semantic_signature: str = ""

    collection_role: str = ""
    navigation_role: str = ""
    surface_role: str = ""
    input_role: str = ""
    execution_role: str = ""

    expected_outcomes: List[str] = Field(default_factory=list)
    ambiguity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    alternative_identities: List[str] = Field(default_factory=list)
    identity_stability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    cross_surface_identity: str = ""
    historical_matches: List[str] = Field(default_factory=list)


class CanonicalOperationalIntent(BaseModel):
    """Intención operacional canónica congelada antes de la transición de UI.

    Ley FFEC: si ``pre_transition_confirmed`` y confianza de entidad ≥ 0.90,
    esta estructura es la única fuente de identidad para identidad de entidad
    seleccionable. UOAC (``operational_affordance``) describe la función
    operacional del elemento; UOEI (``operational_element_identity``) describe
    qué tipo de elemento operacional es. La etiqueta UIA/DOM no es la intención.
    """

    model_config = ConfigDict(extra="ignore")

    intent_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    source_event_id: str = ""

    entity: Optional[OperationalFreezeEntityCandidate] = None
    collection: Optional[OperationalFreezeCollectionSnapshot] = None

    operational_intent_kind: str = ""
    operational_affordance: Optional[OperationalAffordance] = None
    operational_element_identity: Optional[OperationalElementIdentity] = None

    freeze_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    pre_transition_confirmed: bool = False

    canonical_truth: bool = False
    truth_origin: CoiTruthOrigin = CoiTruthOrigin.NONE

    transition_expected: bool = False
    transition_observed: bool = False

    allow_legacy_reinterpretation: bool = False
    allow_after_state_identity: bool = False

    truth_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    truth_reasons: List[str] = Field(default_factory=list)

    resolution_policy: CoiResolutionPolicy = (
        CoiResolutionPolicy.FREEZE_ENTITY_COLLECTION
    )
    execution_policy: CoiExecutionPolicy = (
        CoiExecutionPolicy.COI_UCES_ERL_LONE_UIA_OCR_VISION_COORDS_EMERGENCY
    )
    validation_policy: CoiValidationPolicy = (
        CoiValidationPolicy.INTENDED_ENTITY_AND_EXPECTED_TRANSITION
    )


# ── Entity Resolution Layer (ERL) — entidades operacionales persistentes ─────
# Promueve targets capturados a entidades elegidas por el humano (perfiles,
# búsquedas, archivos, tickets, etc.). Agnóstico de producto/app.


class OperationalEntityType(str, Enum):
    USER_PROFILE = "user_profile"
    ACCOUNT = "account"
    BROWSER_PROFILE = "browser_profile"
    SEARCH_QUERY = "search_query"
    FILE = "file"
    FOLDER = "folder"
    EMAIL = "email"
    CONVERSATION = "conversation"
    TICKET = "ticket"
    CUSTOMER = "customer"
    RESULT_ITEM = "result_item"
    TAB = "tab"
    DOCUMENT = "document"
    PLAYLIST = "playlist"
    UNKNOWN = "unknown"


class OperationalEntityEvidence(BaseModel):
    """Evidencia estructural usada para resolver/promover una entidad."""

    model_config = ConfigDict(extra="ignore")

    uia_name: str = ""
    automation_id: str = ""
    nearby_text: List[str] = Field(default_factory=list)
    parent_chain: List[str] = Field(default_factory=list)
    ocr_text: str = ""
    visual_fingerprint_available: bool = False
    visual_fingerprint_score: float = Field(default=0.0, ge=0.0, le=1.0)
    target_identity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    execution_truth_confirmed: bool = False
    historical_success_score: float = Field(default=0.0, ge=0.0, le=1.0)
    semantic_context: Dict[str, Any] = Field(default_factory=dict)
    window_title: str = ""
    process_name: str = ""


# ── Universal Collection & Entity Semantics (UCES) ─────────────────────────
# Colecciones operacionales visibles (listas, grids, tabs, menús, etc.) y la
# entidad concreta que el humano eligió dentro de ellas.


class OperationalCollectionType(str, Enum):
    LIST = "list"
    GRID = "grid"
    CARDS = "cards"
    TABLE = "table"
    MENU = "menu"
    SEARCH_RESULTS = "search_results"
    TABS = "tabs"
    TREE = "tree"
    SIDEBAR = "sidebar"
    DASHBOARD = "dashboard"
    INBOX = "inbox"
    PLAYLIST = "playlist"
    FILE_BROWSER = "file_browser"
    UNKNOWN = "unknown"


class CollectionEntityReference(BaseModel):
    """Referencia a una entidad dentro de una colección operacional."""

    model_config = ConfigDict(extra="ignore")

    entity_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    entity_type: OperationalEntityType = OperationalEntityType.UNKNOWN
    display_name: str = ""
    normalized_name: str = ""
    position_hint: Dict[str, Any] = Field(default_factory=dict)
    nearby_entities: List[str] = Field(default_factory=list)
    collection_role: str = ""
    selection_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    continuity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: OperationalEntityEvidence = Field(default_factory=OperationalEntityEvidence)


class OperationalCollection(BaseModel):
    """Colección operacional visible (lista, grid, tabs, menú, etc.)."""

    model_config = ConfigDict(extra="ignore")

    collection_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    collection_type: OperationalCollectionType = OperationalCollectionType.UNKNOWN
    display_name: str = ""
    visible_entity_count: int = 0
    entities: List[CollectionEntityReference] = Field(default_factory=list)
    structural_signals: Dict[str, Any] = Field(default_factory=dict)
    continuity_signals: Dict[str, Any] = Field(default_factory=dict)
    selection_patterns: List[str] = Field(default_factory=list)
    scrollable: bool = False
    active_entity_id: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: str = ""
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class OperationalEntity(BaseModel):
    """Entidad operacional persistente elegida por el humano."""

    model_config = ConfigDict(extra="ignore")

    entity_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    entity_type: OperationalEntityType = OperationalEntityType.UNKNOWN
    display_name: str = ""
    normalized_name: str = ""
    aliases: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: OperationalEntityEvidence = Field(default_factory=OperationalEntityEvidence)
    resolution_strategies: List[str] = Field(default_factory=list)
    historical_matches: List[str] = Field(default_factory=list)
    operational_role: str = ""
    replay_anchor_candidates: List[str] = Field(default_factory=list)
    learned_patterns: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ── Live Operational Navigation Engine (LONE) ─────────────────────────────
# Grafos de navegación operacional (ONG) para reencontrar entidades en
# espacios vivos (scroll, lazy load, virtualización, re-render).


class OperationalNavigationAction(str, Enum):
    SCROLL_DOWN = "scroll_down"
    SCROLL_UP = "scroll_up"
    PAGINATE_NEXT = "paginate_next"
    PAGINATE_PREVIOUS = "paginate_previous"
    EXPAND_SECTION = "expand_section"
    COLLAPSE_SECTION = "collapse_section"
    SWITCH_TAB = "switch_tab"
    REOPEN_COLLECTION = "reopen_collection"
    FOCUS_SEARCH = "focus_search"
    REFINE_QUERY = "refine_query"
    CLEAR_FILTER = "clear_filter"
    RETRY_LOAD = "retry_load"
    REOPEN_CONTEXT = "reopen_context"
    WAIT_DYNAMIC_CONTENT = "wait_dynamic_content"
    NAVIGATE_BACK = "navigate_back"
    NAVIGATE_FORWARD = "navigate_forward"
    REFRESH_SURFACE = "refresh_surface"
    REPOSITION_VIEWPORT = "reposition_viewport"
    UNKNOWN = "unknown"


class OperationalNavigationNode(BaseModel):
    """Estado operacional observado en un espacio vivo."""

    model_config = ConfigDict(extra="ignore")

    node_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    collection_signature: str = ""
    visible_entities: List[str] = Field(default_factory=list)
    viewport_signature: str = ""
    active_context: Dict[str, Any] = Field(default_factory=dict)
    navigation_affordances: List[str] = Field(default_factory=list)
    continuity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    structural_hash: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class OperationalNavigationEdge(BaseModel):
    """Transición navegacional entre estados operacionales."""

    model_config = ConfigDict(extra="ignore")

    edge_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    source_node: str = ""
    target_node: str = ""
    navigation_action: OperationalNavigationAction = OperationalNavigationAction.UNKNOWN
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    observed_effect: str = ""
    success_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    latency: float = Field(default=0.0, ge=0.0)


class OperationalNavigationGoal(BaseModel):
    """Objetivo de recuperación en espacio operacional vivo."""

    model_config = ConfigDict(extra="ignore")

    target_entity_id: str = ""
    target_collection_id: str = ""
    target_operational_context: Dict[str, Any] = Field(default_factory=dict)
    target_semantic_intent: str = ""
    recovery_priority: float = Field(default=0.5, ge=0.0, le=1.0)
    max_navigation_depth: int = Field(default=12, ge=1, le=64)
    success_criteria: Dict[str, Any] = Field(default_factory=dict)


# ── Unified Operational Orchestration Layer (UOOL) ───────────────────────────
# Modelo operacional unificado: un solo objetivo, entidad, contexto y
# autoridad decisoria para todas las capas inteligentes (UCES, ERL, LONE,
# ARL, LOP, SLL, OMPC, TEL, GOOR, truth, ambiguity).


class OperationalExecutionPhase(str, Enum):
    PERCEPTION = "perception"
    INTERPRETATION = "interpretation"
    NAVIGATION = "navigation"
    LOCALIZATION = "localization"
    VALIDATION = "validation"
    EXECUTION = "execution"
    RECOVERY = "recovery"
    REPAIR = "repair"
    CONTINUITY = "continuity"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    UNSAFE = "unsafe"


class DominantOperationalStrategy(str, Enum):
    ENTITY_FIRST = "entity_first"
    COLLECTION_NAVIGATION = "collection_navigation"
    CONTINUITY_RECOVERY = "continuity_recovery"
    SEMANTIC_RELOCATION = "semantic_relocation"
    VALIDATOR_DRIVEN = "validator_driven"
    OPERATIONAL_MEMORY_REUSE = "operational_memory_reuse"
    WAIT_DYNAMIC_CONTENT = "wait_dynamic_content"
    EXECUTE_STEP = "execute_step"
    FALLBACK_SAFE = "fallback_safe"


class OperationalActionKind(str, Enum):
    EXECUTE = "execute"
    NAVIGATE = "navigate"
    WAIT = "wait"
    RECOVER = "recover"
    REPAIR = "repair"
    ASK_HUMAN = "ask_human"
    STOP = "stop"
    FALLBACK = "fallback"


class UnifiedOperationalMemorySignals(BaseModel):
    """Señales de memoria cruzada (SLL, OMPC, navegación, entidad, colección)."""

    model_config = ConfigDict(extra="ignore")

    sll_strategy_hints: List[str] = Field(default_factory=list)
    sll_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    ompc_pattern_keys: List[str] = Field(default_factory=list)
    ompc_boost: float = Field(default=0.0, ge=0.0, le=1.0)
    navigation_memory_hits: List[str] = Field(default_factory=list)
    entity_memory_hits: List[str] = Field(default_factory=list)
    collection_memory_hits: List[str] = Field(default_factory=list)
    memory_agreement: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: List[str] = Field(default_factory=list)


class OperationalRecoveryBudget(BaseModel):
    """Presupuesto anti-storm para recovery / repair / navegación."""

    model_config = ConfigDict(extra="ignore")

    recovery_budget: int = Field(default=4, ge=0, le=64)
    repair_budget: int = Field(default=2, ge=0, le=32)
    navigation_budget: int = Field(default=8, ge=0, le=128)
    recovery_used: int = Field(default=0, ge=0)
    repair_used: int = Field(default=0, ge=0)
    navigation_used: int = Field(default=0, ge=0)
    revalidation_used: int = Field(default=0, ge=0)
    max_revalidation: int = Field(default=3, ge=0, le=16)

    @property
    def recovery_exhausted(self) -> bool:
        return self.recovery_used >= self.recovery_budget

    @property
    def repair_exhausted(self) -> bool:
        return self.repair_used >= self.repair_budget

    @property
    def navigation_exhausted(self) -> bool:
        return self.navigation_used >= self.navigation_budget

    @property
    def revalidation_exhausted(self) -> bool:
        return self.revalidation_used >= self.max_revalidation


class UnifiedOperationalExecutionContext(BaseModel):
    """Contexto operacional unificado (UOEC) — objetivo, entidad, fase y señales."""

    model_config = ConfigDict(extra="ignore")

    objective_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    operational_goal: str = ""
    target_entity: Optional[Dict[str, Any]] = None
    target_collection: Optional[Dict[str, Any]] = None
    active_surface: str = ""
    current_phase: OperationalExecutionPhase = OperationalExecutionPhase.PERCEPTION
    dominant_strategy: DominantOperationalStrategy = DominantOperationalStrategy.EXECUTE_STEP
    runtime_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    continuity_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    navigation_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    execution_truth: bool = False
    current_constraints: List[str] = Field(default_factory=list)
    active_blockers: List[str] = Field(default_factory=list)
    memory_signals: UnifiedOperationalMemorySignals = Field(
        default_factory=UnifiedOperationalMemorySignals,
    )
    live_perception: Optional[Dict[str, Any]] = None
    adaptation_state: Dict[str, Any] = Field(default_factory=dict)
    budget: OperationalRecoveryBudget = Field(
        default_factory=OperationalRecoveryBudget,
    )
    orchestration_trace: List[Dict[str, Any]] = Field(default_factory=list)
    step_index: int = Field(default=0, ge=0)
    step_id: str = ""
    unified_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class OperationalActionDecision(BaseModel):
    """Decisión única de UOOL — ninguna capa debe decidir globalmente sola."""

    model_config = ConfigDict(extra="ignore")

    action: OperationalActionKind = OperationalActionKind.EXECUTE
    dominant_strategy: DominantOperationalStrategy = DominantOperationalStrategy.EXECUTE_STEP
    phase: OperationalExecutionPhase = OperationalExecutionPhase.EXECUTION
    human_message: str = ""
    expert_detail: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    block_autonomous_recovery: bool = False
    allow_entity_first: bool = True
    allow_navigation: bool = True
    allow_arl_recovery: bool = True
    reason_codes: List[str] = Field(default_factory=list)


# ── Operational Convergence & Repeatability Program (OCRP) ───────────────────
# Convergencia operacional real: estabilidad, repetibilidad y anti-jitter.


class OperationalConvergenceState(BaseModel):
    """Medición de estabilidad/convergencia del runtime operacional."""

    model_config = ConfigDict(extra="ignore")

    strategy_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    recovery_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    navigation_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    entity_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    collection_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    runtime_variance: float = Field(default=0.0, ge=0.0, le=1.0)
    validator_consistency: float = Field(default=0.0, ge=0.0, le=1.0)
    perception_agreement: float = Field(default=0.0, ge=0.0, le=1.0)
    orchestration_coherence: float = Field(default=0.0, ge=0.0, le=1.0)
    retry_convergence: float = Field(default=0.0, ge=0.0, le=1.0)
    timing_consistency: float = Field(default=0.0, ge=0.0, le=1.0)
    convergence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    instability_flags: List[str] = Field(default_factory=list)
    detectors_fired: List[str] = Field(default_factory=list)
    unstable_step_indices: List[int] = Field(default_factory=list)


class AdaptiveRuntimeTimingModel(BaseModel):
    """Timing adaptativo aprendido de convergencia histórica (no sleeps fijos)."""

    model_config = ConfigDict(extra="ignore")

    wait_ms: int = Field(default=800, ge=0, le=30_000)
    revalidate_after_ms: int = Field(default=1200, ge=0, le=60_000)
    navigation_settle_ms: int = Field(default=350, ge=0, le=15_000)
    abandon_after_ms: int = Field(default=12_000, ge=500, le=120_000)
    post_action_settle_ms: int = Field(default=400, ge=0, le=15_000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: str = "default"


class OperationalExecutionSignature(BaseModel):
    """Huella de una ejecución para comparación multi-run."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = ""
    mission_id: str = ""
    mission_version: int = 1
    status: str = ""
    dominant_strategy_sequence: List[str] = Field(default_factory=list)
    recovery_path: List[str] = Field(default_factory=list)
    navigation_path: List[str] = Field(default_factory=list)
    validator_outcomes: List[Dict[str, Any]] = Field(default_factory=list)
    timing_profile: Dict[str, float] = Field(default_factory=dict)
    step_outcomes: List[str] = Field(default_factory=list)
    convergence_fingerprint: str = ""
    repeatability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    duration_ms: float = Field(default=0.0, ge=0.0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class StepStabilityIndex(BaseModel):
    """Índice de estabilidad por step operacional."""

    model_config = ConfigDict(extra="ignore")

    step_index: int = Field(default=0, ge=0)
    step_id: str = ""
    replay_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    entity_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    navigation_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    validator_stability: float = Field(default=0.0, ge=0.0, le=1.0)
    runtime_variance: float = Field(default=0.0, ge=0.0, le=1.0)
    stability_index: float = Field(default=0.0, ge=0.0, le=1.0)
    fragile: bool = False
    notes: List[str] = Field(default_factory=list)


class OperationalStabilizationHints(BaseModel):
    """Hints de estabilización para UOOL / SmartExecutor."""

    model_config = ConfigDict(extra="ignore")

    frozen_strategy: Optional[str] = None
    max_strategy_switches: int = Field(default=2, ge=0, le=16)
    max_arl_retries: int = Field(default=1, ge=0, le=8)
    max_navigation_branching: int = Field(default=3, ge=0, le=32)
    prefer_continuity: bool = True
    block_recovery: bool = False
    block_navigation: bool = False
    adaptive_wait_ms: int = Field(default=800, ge=0, le=30_000)
    reason_codes: List[str] = Field(default_factory=list)


# ── Four-Layer Operational Model (4LOM) ─────────────────────────────────────
# Goal → propósito | Contract → éxito | Human View → edición | Machine → ejecución
# Ninguna capa sustituye a otra; se enlazan por ``machine_step_id`` estable.


class OperationalGoalLayer(BaseModel):
    """Capa 1 — propósito de la automatización (inmutable ante edits de máquina)."""

    model_config = ConfigDict(extra="ignore")

    goal_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    purpose_statement: str = ""
    summary: str = ""
    source: str = "inferred"
    version: int = 1


class OperationalContractCriterion(BaseModel):
    """Criterio observable de éxito (capa Contract)."""

    model_config = ConfigDict(extra="ignore")

    code: str
    description: str = ""
    required: bool = True


class OperationalContractLayer(BaseModel):
    """Capa 2 — contrato de éxito (READY, validadores, blockers)."""

    model_config = ConfigDict(extra="ignore")

    contract_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    success_criteria: List[OperationalContractCriterion] = Field(default_factory=list)
    validator_codes: List[str] = Field(default_factory=list)
    intent_status: str = ""
    not_ready_reasons: List[str] = Field(default_factory=list)
    ready_provenance: str = ""
    version: int = 1


class HumanViewStep(BaseModel):
    """Paso editable en Mission Review — enlazado a ejecución por ``machine_step_id``."""

    model_config = ConfigDict(extra="ignore")

    machine_step_id: str
    display_index: int = 0
    human_label: str = ""
    needs_review: bool = False
    confirmed: bool = False
    hidden: bool = False
    disabled: bool = False
    user_notes: str = ""
    badges: List[str] = Field(default_factory=list)


class HumanViewLayer(BaseModel):
    """Capa 3 — presentación y edición humana (sin mutar tipo/params de ejecución)."""

    model_config = ConfigDict(extra="ignore")

    view_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    steps: List[HumanViewStep] = Field(default_factory=list)
    version: int = 1


# ── Operational State Understanding Engine (OSUE) ───────────────────────────


class OperationalStateType(str, Enum):
    """Estados operacionales universales inferidos por OSUE."""

    # READY
    LAUNCHER_READY_STATE = "launcher_ready_state"
    SEARCH_INPUT_READY_STATE = "search_input_ready_state"
    RESULTS_READY_STATE = "results_ready_state"
    NAVIGATION_READY_STATE = "navigation_ready_state"
    INTERACTION_READY_STATE = "interaction_ready_state"
    # TRANSITION
    LOADING_STATE = "loading_state"
    NAVIGATION_TRANSITION_STATE = "navigation_transition_state"
    CONTEXT_SWITCH_STATE = "context_switch_state"
    ASYNC_REFRESH_STATE = "async_refresh_state"
    # BLOCKED
    AUTH_REQUIRED_STATE = "auth_required_state"
    MODAL_INTERRUPTION_STATE = "modal_interruption_state"
    DESTRUCTIVE_CONFIRMATION_STATE = "destructive_confirmation_state"
    OVERLAY_BLOCKING_STATE = "overlay_blocking_state"
    RUNTIME_ERROR_STATE = "runtime_error_state"
    # CONTENT
    EMPTY_RESULTS_STATE = "empty_results_state"
    RESULTS_LOADING_STATE = "results_loading_state"
    RESULTS_AVAILABLE_STATE = "results_available_state"
    INFINITE_SCROLL_STATE = "infinite_scroll_state"
    # UNKNOWN
    UNKNOWN_OPERATIONAL_STATE = "unknown_operational_state"


class OperationalStateSnapshot(BaseModel):
    """Instantánea OSUE — estado operacional real inferido."""

    model_config = ConfigDict(extra="ignore")

    state_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    operational_state_type: OperationalStateType = OperationalStateType.UNKNOWN_OPERATIONAL_STATE
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    active_goal: str = ""
    active_operational_thread: str = ""
    visible_operational_elements: List[str] = Field(default_factory=list)
    active_surface_types: List[str] = Field(default_factory=list)
    continuity_state: str = ""
    loading_state: str = ""
    interaction_readiness: str = ""
    navigation_state: str = ""
    auth_state: str = ""
    modal_state: str = ""
    results_state: str = ""
    error_state: str = ""
    focus_state: str = ""
    runtime_blockers: List[str] = Field(default_factory=list)
    expected_transitions: List[str] = Field(default_factory=list)
    observed_transitions: List[str] = Field(default_factory=list)
    state_stability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    ambiguity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    state_lineage: List[str] = Field(default_factory=list)
    historical_matches: List[str] = Field(default_factory=list)


# ── Operational Runtime Consistency Engine (ORCE) ───────────────────────────


class OperationalExecutionExpectation(BaseModel):
    """Contrato de ejecución esperado para un paso Machine (pre-runtime)."""

    model_config = ConfigDict(extra="ignore")

    expected_runtime_path: List[str] = Field(default_factory=list)
    preferred_strategies: List[str] = Field(default_factory=list)
    forbidden_strategies: List[str] = Field(default_factory=list)
    fallback_budget: int = Field(default=1, ge=0, le=8)
    max_retries: int = Field(default=1, ge=0, le=8)
    expected_surface_types: List[str] = Field(default_factory=list)
    expected_transition_chain: List[str] = Field(default_factory=list)
    required_operational_states: List[str] = Field(default_factory=list)
    expected_state_transitions: List[str] = Field(default_factory=list)
    blocked_operational_states: List[str] = Field(default_factory=list)
    expected_validation_behavior: str = "must_pass_on_success"
    expected_runtime_latency_ms: int = Field(default=4000, ge=0, le=120_000)
    canonical_truth_required: bool = False
    continuity_required: bool = True


class OperationalRuntimeObservation(BaseModel):
    """Observación real de ejecución (post-runtime, por paso)."""

    model_config = ConfigDict(extra="ignore")

    step_id: str = ""
    actual_runtime_path: List[str] = Field(default_factory=list)
    actual_strategies_used: List[str] = Field(default_factory=list)
    retries_used: int = 0
    repairs_triggered: List[str] = Field(default_factory=list)
    coords_used: bool = False
    smart_route_used: bool = False
    fallback_used: bool = False
    validation_failures: int = 0
    runtime_surface_chain: List[str] = Field(default_factory=list)
    runtime_transition_chain: List[str] = Field(default_factory=list)
    runtime_operational_state_chain: List[str] = Field(default_factory=list)
    expected_state_transitions: List[str] = Field(default_factory=list)
    actual_state_transitions: List[str] = Field(default_factory=list)
    state_mismatch: bool = False
    state_wait_timeout: bool = False
    blocked_state_detected: bool = False
    osue_wait_reason: str = ""
    runtime_latency_ms: int = 0
    divergence_detected: bool = False
    divergence_reasons: List[str] = Field(default_factory=list)
    consistency_score: float = Field(default=1.0, ge=0.0, le=1.0)
    degradation_level: str = "none"
    runtime_thread_id: str = ""


class OperationalRuntimeConsistencyReport(BaseModel):
    """Reconciliación ORCE agregada (misión)."""

    model_config = ConfigDict(extra="ignore")

    consistency_status: str = "NO_DATA"
    consistency_score: float = Field(default=0.0, ge=0.0, le=1.0)
    divergence_count: int = 0
    forbidden_strategy_usage: int = 0
    hidden_fallbacks: int = 0
    hidden_repairs: int = 0
    runtime_degradations: List[str] = Field(default_factory=list)
    canonical_truth_violations: int = 0
    continuity_breaks: int = 0
    weakest_runtime_steps: List[Dict[str, Any]] = Field(default_factory=list)
    runtime_acceptance: str = "NO_DATA"
    runtime_repair_suggestions: List[str] = Field(default_factory=list)
    step_summaries: List[Dict[str, Any]] = Field(default_factory=list)


class AdaptiveExecutionVariant(BaseModel):
    """Variante mecánica sintetizada por ARSS — compatible con Goal/Contract."""

    model_config = ConfigDict(extra="ignore")

    variant_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    parent_machine_step_id: str = ""
    goal_id: str = ""
    operational_element_identity: Optional[OperationalElementIdentity] = None

    synthesized_runtime_path: List[str] = Field(default_factory=list)
    synthesized_strategies: List[str] = Field(default_factory=list)
    synthesized_surface_chain: List[str] = Field(default_factory=list)
    synthesized_validation_path: List[str] = Field(default_factory=list)

    synthesis_reason: str = ""
    generated_from_divergence: bool = False
    generated_from_runtime_observation: bool = False

    consistency_score: float = Field(default=0.0, ge=0.0, le=1.0)
    replay_stability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    deterministic_score: float = Field(default=0.0, ge=0.0, le=1.0)
    validation_success_ratio: float = Field(default=0.0, ge=0.0, le=1.0)

    continuity_preserved: bool = False
    canonical_truth_preserved: bool = True
    forbidden_strategy_used: bool = False

    accepted_for_runtime: bool = False
    promoted_to_machine_layer: bool = False
    usage_count: int = 0
    successful_runs: int = 0
    failed_runs: int = 0


class AdaptiveSynthesisAttempt(BaseModel):
    """Intento de síntesis ARSS tras divergencia ORCE."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    divergence_source: str = ""
    failed_runtime_path: List[str] = Field(default_factory=list)
    observed_surface_state: str = ""
    expected_contract: Dict[str, Any] = Field(default_factory=dict)

    generated_variants: List[AdaptiveExecutionVariant] = Field(default_factory=list)
    rejected_variants: List[AdaptiveExecutionVariant] = Field(default_factory=list)
    accepted_variant: Optional[AdaptiveExecutionVariant] = None

    synthesis_strategy: str = ""
    replay_validation_results: Dict[str, Any] = Field(default_factory=dict)


class MachineExecutionStep(BaseModel):
    """Capa 4 — paso ejecutable (SEP); editable sin tocar Goal."""

    model_config = ConfigDict(extra="ignore")

    step_id: str
    semantic_type: str
    params: Dict[str, Any] = Field(default_factory=dict)
    preferred_strategy: str = ""
    fallback_strategies: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    block_id: Optional[str] = None
    disabled: bool = False
    runtime_expectation: Optional[OperationalExecutionExpectation] = None
    runtime_allowed_degradations: List[str] = Field(default_factory=list)


class MachineExecutionLayer(BaseModel):
    """Capa 4 — plan de ejecución para runtime."""

    model_config = ConfigDict(extra="ignore")

    execution_id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    steps: List[MachineExecutionStep] = Field(default_factory=list)
    source: str = "semantic_execution_plan"
    version: int = 1
    coords_used: bool = False
    legacy_graph_ignored: bool = True


class FourLayerOperationalModel(BaseModel):
    """Modelo operacional en cuatro capas desacopladas."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1"
    goal: OperationalGoalLayer = Field(default_factory=OperationalGoalLayer)
    contract: OperationalContractLayer = Field(default_factory=OperationalContractLayer)
    human_view: HumanViewLayer = Field(default_factory=HumanViewLayer)
    machine_execution: MachineExecutionLayer = Field(
        default_factory=MachineExecutionLayer,
    )
    last_synced_at: str = ""


# ============================================================================
# Objeto Principal Mission
# ============================================================================

class Mission(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4()))
    name: str
    version: int = 1
    schema_version: str = "2.0" # Ultra RPA Schema
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: MissionStatus = MissionStatus.DRAFT
    
    # 1. Grabación
    raw_trace: List[RawEvent] = Field(default_factory=list, description="Eventos atómicos capturados")
    user_annotations: List[Annotation] = Field(default_factory=list)
    
    # 2. Interpretación Semántica
    interpreted_steps: List[InterpretedStep] = Field(default_factory=list)
    
    # 3. Misión Compilada (Ejecutable)
    compiled_execution_graph: List[CompiledStep] = Field(default_factory=list)

    # 3.b Plan semántico — fuente única para Smart/AutoLearningExecutor.
    #
    # Producido por ``build_semantic_execution_plan`` a partir del Intent
    # Collapse Engine. Si está presente, ``smart_runner_bridge`` lo usa y
    # **ignora** ``compiled_execution_graph`` (que sigue ahí solo como
    # detalle de auditoría / vista expert). Estructura serializada:
    #
    #   {
    #     "source": "intent_collapse_engine",
    #     "version": 1,
    #     "average_confidence": 0.92,
    #     "coords_used": false,
    #     "legacy_graph_ignored": true,
    #     "steps": [
    #       {"id": "...", "type": "open_app", "params": {...},
    #        "preferred_strategy": "open_app:windows_search",
    #        "fallback_strategies": [...], "confidence": 0.95,
    #        "human_label": "Abrir Chrome"},
    #       ...
    #     ],
    #   }
    #
    # Mantenemos un dict (en vez de un modelo Pydantic anidado) para
    # evitar acoplamientos circulares entre contracts y services.
    semantic_execution_plan: Optional[Dict[str, Any]] = None

    # Four-Layer Operational Model — Goal / Contract / Human View / Machine.
    # Permite editar Human View sin romper Machine Steps, y Machine sin Goal.
    four_layer_operational_model: Optional[Dict[str, Any]] = Field(
        default=None,
        description="4LOM serializado (FourLayerOperationalModel).",
    )

    # 3.c Propósito del compiled_execution_graph (PRD 2026-05-07).
    #
    # Cuando ``semantic_execution_plan`` está presente y es ejecutable,
    # el grafo legacy ya NO se ejecuta — solo se conserva para vista
    # expert / debug / forense. Lo marcamos explícitamente con este
    # campo para que la UI y el smart_runner_bridge puedan decidir sin
    # ambigüedad qué grafo usar.
    #
    # Valores:
    #   * ``"execution"``         — grafo en uso (no hay plan semántico).
    #   * ``"legacy_debug_only"`` — hay plan semántico, grafo solo debug.
    legacy_compiled_graph_purpose: str = "execution"

    # 4. Orquestación
    triggers: List[TriggerDefinition] = Field(default_factory=list)
    recovery_rules: Dict[str, Any] = Field(default_factory=dict)
    validation_rules: Dict[str, Any] = Field(default_factory=dict)
    policy: MissionPolicy = Field(default_factory=MissionPolicy)
    
    
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list, description="Nombres alternativos para invocar esta misión")

    action_groups: List["ActionGroup"] = Field(default_factory=list)

    # Audit trail de Mission Review (PRD 2026-05-08 §H).
    # Cada entrada registra qué dato faltaba en el raw_trace y cómo
    # fue resuelto (usuario lo escribió, aceptamos sugerencia
    # heurística, o lo recuperamos de un alias aprendido). El
    # ``raw_trace`` NUNCA se modifica — esto vive aparte para
    # mantener la traza forense intacta.
    review_confirmations: List["ReviewConfirmation"] = Field(
        default_factory=list,
    )

    # Audit trail de Regrabación de paso (PRD 2026-05-08c §E + §F).
    # Cada entrada describe una sesión completa "el usuario regrabó
    # el paso N": qué se reemplazó, qué pasos previos se reprodujeron
    # para llegar al contexto, qué pasos posteriores se vieron
    # afectados (si los hay) y los timestamps. Solo se persiste
    # cuando ``status="saved"`` — sesiones canceladas no quedan aquí
    # (no contaminan el JSON con basura). Ver
    # ``app.services.missions.rerecord_session.RerecordOrchestrator``.
    rerecord_history: List["RerecordHistoryEntry"] = Field(
        default_factory=list,
    )

    # Audit trail del Semantic Intent Promotion Engine
    # (PRD 2026-05-14 §SIPE-Persistence). Cada entrada describe UNA
    # promoción legacy → genérico aplicada por SIPE durante el último
    # ``apply_promotion_to_mission`` (en runtime o vía bulk migration).
    #
    # Estructura (serializable, generada por SIPE):
    #   {
    #     "from": "search_youtube",
    #     "to":   "search_site",
    #     "reason": "search_site_canonical",
    #     "step_index": 4,
    #     "human_label": "Buscar \"…\" en YouTube",
    #     "site": "youtube"   # cuando aplica
    #   }
    #
    # NO lo modifica nadie aparte de SIPE. La UI experta lo expone
    # en Mission Review (modo debug) y los tests post-mortem lo
    # consultan para auditar reglas de promoción.
    intent_promotions: List[Dict[str, Any]] = Field(
        default_factory=list,
    )

    # Códigos de bloqueo emitidos por SIPE en la última promoción
    # (instancias de :class:`PromotionBlockerCode`). Sirve para
    # entender por qué un SEP quedó NEEDS_REVIEW aún después de
    # promover (ej. ``query_fidelity_low``, ``profile_unknown``).
    # Lista de strings cortos — NUNCA contienen PII de la grabación.
    intent_promotion_blockers: List[str] = Field(
        default_factory=list,
    )

    # ── Semantic-first recorder (Fase 1, modo shadow) ─────────────────
    # Timeline de hipótesis capturadas durante la grabación. Las misiones
    # antiguas sin esta clave cargan con lista vacía (compat JSON).
    intent_timeline: List[SemanticHypothesis] = Field(default_factory=list)
    intent_snapshots: List[IntentSnapshot] = Field(default_factory=list)
    semantic_first_version: Optional[str] = None
    semantic_first_mode: Optional[str] = None  # e.g. "shadow"
    # Diagnóstico opcional (Mission Review experto / tests). No afecta ejecución.
    semantic_shadow_audit: Optional[Dict[str, Any]] = None

    # ── Semantic Session Engine (observación / sombra) ────────────────────
    semantic_sessions: List[SemanticSession] = Field(default_factory=list)
    operational_intents: List[OperationalIntent] = Field(default_factory=list)
    # False por defecto: misiones legacy sin clave no activan el motor hasta grabación nueva.
    session_shadow_mode: bool = Field(default=False)
    semantic_session_audit: Optional[Dict[str, Any]] = None

    # ── Freeze-First Execution Core (FFEC) — intenciones canónicas por evento ──
    canonical_operational_intents: List[CanonicalOperationalIntent] = Field(
        default_factory=list,
        description=(
            "Verdad operacional promovida desde PCOF (pre-transición). "
            "Inmutable frente a after_state / legacy ambiguity."
        ),
    )

    operational_truth_chain: Optional[OperationalTruthChain] = Field(
        default=None,
        description=(
            "Cadena OTP: preserved / degraded / lost / transformed y "
            "loss_by_stage para auditoría Mission Review experto."
        ),
    )

    operational_continuity_chain: Optional[OperationalContinuityChain] = Field(
        default=None,
        description=(
            "OCRX: hilos operacionales, bridges de transición de superficie "
            "y continuity_preserved para gate/runtime."
        ),
    )

    # ── Canonical Operational Blocks (Session Promotion Layer, Fase 1 shadow) ──
    canonical_operational_blocks: List[CanonicalOperationalBlock] = Field(
        default_factory=list,
        description="Bloques operacionales canónicos derivados de sesiones semánticas.",
    )
    # Si False: no se generan ni actualizan COB (JSON legacy sin clave = False).
    cob_shadow_mode: bool = Field(default=False)
    cob_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Diagnóstico COB vs SEP / compresión (solo sombra).",
    )
    # Simulación de replay por COB (runtime shadow). No controla mouse/teclado real.
    cob_replay_shadow: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último snapshot de ReplayPlan por COB + comparación vs SEP/legacy.",
    )
    # Adaptador runtime COB → executor (solo sombra; no ejecuta ni sustituye SmartExecutor).
    cob_runtime_adapter_shadow: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Traducción UIC/executor + cobertura vs SEP (shadow-safe).",
    )
    # Dry-run bridge: validación registry/validator/safety/SEP sobre adapter shadow.
    cob_dry_run_shadow: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Resultado dry-run COB adapter vs UIC + SEP (sin ejecución).",
    )
    # COB Execution Pilot (Fase 1): ejecución real acotada + auditoría; no sustituye SEP.
    cob_execution_pilot_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditoría del piloto COB (eligibility, ejecución, fallback SEP, deltas).",
    )
    # Adaptive Runtime Layer (Fase 1): observabilidad SAFE; no sustituye SmartExecutor ni SEP.
    adaptive_runtime_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Eventos y decisiones del ARL (desviaciones, relocation, continuidad, métricas).",
    )

    # Universal Operational Language — telemetría de ejecución runtime (por step).
    uol_runtime_audit: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Auditoría UOL-native: handler, fallback legacy, coords, smart_route, "
            "validación y resolution_path por step."
        ),
    )
    uol_runtime_acceptance_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último UolRuntimeAcceptanceReport serializado (ratio native, coords, estado).",
    )

    # ORCE — consistencia runtime vs 4LOM Machine/Contract.
    operational_runtime_consistency_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último OperationalRuntimeConsistencyReport (expected vs actual, acceptance).",
    )
    orce_runtime_audit: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Observaciones ORCE por paso (persistencia de divergencias).",
    )

    # ARSS — variantes mecánicas adaptativas (Goal/Contract intactos).
    arss_runtime_variants: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Variantes ARSS aceptadas/promovidas por paso machine.",
    )
    arss_synthesis_audit: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Intentos de síntesis ARSS tras divergencias ORCE.",
    )

    # Entity Resolution Layer — auditoría de resolución en runtime (por step).
    entity_runtime_resolution_audit: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Intentos ERL en ejecución: entidad, confianza, estrategia, fallback.",
    )

    # UCES — auditoría colección→entidad en grabación y runtime.
    collection_entity_resolution_audit: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Detección de colección, selección de entidad, matching y continuidad.",
    )

    # LONE — navegación en espacios operacionales vivos (ONG, recovery).
    operational_navigation_audit: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Decisiones LONE: nodos ONG, acciones de navegación, continuidad.",
    )

    # Truth Extraction Layer (Fase 1): sombra; verdad operacional humana estable.
    truth_blocks: List[OperationalTruthBlock] = Field(
        default_factory=list,
        description="Bloques TEL tras ventana de estabilización / consolidación contextual.",
    )
    tel_shadow_mode: bool = Field(
        default=False,
        description="Si True la grabación pobla truth_blocks sin sustituir SEP.",
    )
    tel_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditoría TEL vs SEP: compresión, ruido absorbido, claridad.",
    )

    # TEL → SEP hybrid promotion (Fase 2)
    tel_semantic_execution_plan_candidate: Optional[Dict[str, Any]] = Field(
        default=None,
        description="SEP candidato desde truth_blocks antes de fusión/decisión.",
    )
    tel_sep_promotion_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditoría comparativa TEL_SEP vs SEP existente.",
    )
    tel_sep_promotion_decision: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Decisión de promoción híbrida (shadow/suggest/primary).",
    )

    # Operational Runtime Graph (Fase ORG shadow): continuidad operacional vs SEP lineal.
    operational_runtime_graph: Optional[Dict[str, Any]] = Field(
        default=None,
        description="ORG serializado (nodos/transiciones/objetivos/recuperaciones) — modo sombra.",
    )
    org_shadow_mode: bool = Field(
        default=False,
        description="Si True la misión permite reconstruir ORG desde fuentes declarativas (no ejecuta grafo real).",
    )
    org_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último snapshot de auditoría ORG vs SEP / métricas de resiliencia (sombra).",
    )

    # Operational Continuity Runtime (OCR) — Fase 1 shadow; no ejecuta ni reemplaza SmartExecutor / SEP.
    operational_continuity_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditoría OCR: drift, recuperaciones ordenadas, comparativa vs SEP, métricas de autonomía.",
    )
    operational_goal_history: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Historia acotada de goal OCR evaluados (headline, progreso, timestamp ISO).",
    )
    continuity_runtime_shadow: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último OperationalContinuitySnapshot serializado (solo sombra / predicción).",
    )

    # Goal-Oriented Operational Runtime (GOOR) — Fase 1 shadow; no ejecuta runtime real ni SEP.
    goal_oriented_runtime_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditoría GOOR: objetivos candidatos, transiciones rankeadas, métricas de autonomía vs replay procedural.",
    )
    operational_goal_snapshots: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Historial GOOR snapshot (OperationalGoalExecutionSnapshot serializado JSON).",
    )
    goor_shadow_mode: bool = Field(
        default=False,
        description="Si True, Mission Review / servicios pueden evaluar GOOR en sombra (no muta ejecutor).",
    )

    # Live Operational Perception (LOP) — Fase 1 sombra: percepción viva sin mutar SEP ni ejecutores.
    live_operational_perception_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditoría LOP: fuentes usadas, presupuesto de tiempo, huecos (sin PII de la pantalla).",
    )
    lop_shadow_mode: bool = Field(
        default=False,
        description="Si True, la misión puede capturar snapshots LOP en sombra (recomendaciones sólo).",
    )
    lop_last_snapshot: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último LiveOperationalPerceptionSnapshot serializado (JSON-friendly).",
    )
    # ── Live Operational Perception · Fase 2 (sensor bridge / live feed shadow) ──
    lop_live_feed_enabled: bool = Field(
        default=False,
        description=(
            "Si True, Mission Review puede llenar LivePerceptionInput vía "
            "`lop_sensor_bridge` (UIA/DOM/validators…) en modo read-only shadow."
        ),
    )
    lop_sensor_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Última auditoría LopSensorAudit (dump JSON-friendly).",
    )
    lop_last_sensor_health: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último LopSensorHealth serializado.",
    )

    # Operational State Understanding Engine (OSUE) — estado operacional real.
    osue_shadow_mode: bool = Field(
        default=False,
        description="Si True, la misión captura OperationalStateSnapshot en sombra.",
    )
    osue_last_snapshot: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último OperationalStateSnapshot serializado (JSON-friendly).",
    )
    osue_state_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditoría OSUE: transiciones, bloqueos, estabilidad (sin PII).",
    )
    osue_state_lineage: List[str] = Field(
        default_factory=list,
        description="Cadena acotada de estados operacionales observados en runtime/replay.",
    )
    osue_runtime_wait_audit: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Auditoría OSUE por paso: waits, timeouts, blockers, transiciones.",
    )

    # One-Shot Reliability Closure — auditoría consolidada (no sustituye SEP ni ejecutores).
    one_shot_reliability_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Último informe one-shot: readiness, plan_source, runtime_mode, "
            "preflight, perception, scores — UI experto / telemetría."
        ),
    )

    # Reliability hardening — latencia / telemetría / salud (opcional; UI experto).
    runtime_latency_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último informe de latencias por sección (smart_runner / percepción / repair).",
    )
    runtime_production_telemetry: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Cola acotada de eventos de telemetría de producción (JSON-friendly).",
    )
    mission_health_snapshot: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último snapshot de mission_health_score (factores + score agregado).",
    )
    beta_private_acceptance_snapshot: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Snapshot ``beta_private_acceptance_score``: estado READY/NEEDS/UNSAFE y factores para beta privada."
        ),
    )
    beta_runtime_acceptance_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último BetaRuntimeAcceptanceReport (UOL/ORCE/OSUE/ARSS unificado).",
    )

    # Unified Operational Orchestration Layer (UOOL) — coordinador único; no sustituye SmartExecutor.
    uool_last_context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último UnifiedOperationalExecutionContext serializado (JSON-friendly).",
    )
    uool_orchestration_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Informe consolidado UOOL: fase, estrategia dominante, confianza, presupuestos.",
    )
    uool_audit_trace: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Traza acotada de decisiones orchestrator (expert mode / telemetría).",
    )

    # Operational Convergence & Repeatability Program (OCRP) — convergencia estadística real.
    ocrp_convergence_state: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Último OperationalConvergenceState serializado.",
    )
    ocrp_repeatability_report: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Informe repeatability: score, multi-run variance, execute-always candidacy.",
    )
    ocrp_execution_signatures: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Historial acotado de OperationalExecutionSignature por run.",
    )
    ocrp_timing_model: Optional[Dict[str, Any]] = Field(
        default=None,
        description="AdaptiveRuntimeTimingModel calibrado por self-calibration.",
    )
    ocrp_calibration_audit: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Auditoría de self-calibration (ajustes waits/thresholds/budgets).",
    )
    ocrp_step_stability: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="StepStabilityIndex por step — weakest steps / fragile paths.",
    )


class MissionExecution(BaseModel):
    """Estado de una corrida de reproducción contra una misión."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(__import__("uuid").uuid4()))
    mission_id: str
    mission_version: int
    status: ReplayStatus = ReplayStatus.PENDING
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    executed_by: str = "agent"
    steps_executed: int = 0
    steps_failed: int = 0
    failed_step_id: Optional[str] = None
    error_details: Optional[str] = None
    understanding: str = ""
    plan: str = ""
    execution_summary: str = ""
    verification_summary: str = ""
    step_run_log: List[Dict[str, Any]] = Field(default_factory=list)
    steps_skipped: int = 0
    steps_manual: int = 0
    steps_recovered_retry: int = 0  # recuperado tras «Reintentar» en mismo paso
    user_recovery_cycles_total: int = 0  # comandos retry acumulados
    had_warnings: bool = False  # skip / quality warning


def mission_to_dict(mission: Mission) -> Dict[str, Any]:
    return mission.model_dump(mode="json")

def mission_from_dict(data: Dict[str, Any]) -> Mission:
    return Mission.model_validate(data)
