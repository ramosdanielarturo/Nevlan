"""ArthurOS Services - Missions package."""

from app.services.missions.store import mission_store, MissionStore
from app.services.missions.recorder import (
    start_recording,
    stop_recording,
    stop_and_compile,
    pause_recording,
    resume_recording,
    get_recorder,
    MissionRecorder,
)
from app.services.missions.player import replay_mission, MissionPlayer
from app.services.missions.target_resolver import (
    TargetResolver, TargetResolutionResult,
)
from app.services.missions.repair import (
    OneClickCapture, capture_target_bundle, apply_repair_to_step,
    record_method_outcome,
)
from app.services.missions.semantic_interpreter import interpret_mission
from app.services.missions.visual_capture import (
    capture_visual_target,
    select_target_bbox,
    compute_asset_bbox,
    CapturedVisual,
    TargetCandidate,
)
from app.services.missions.semantic_fusion import (
    apply_semantic_fusion,
    confirm_step_label,
)
from app.services.missions.asset_finalizer import (
    finalize_mission_assets,
    has_temp_paths,
    enrich_resolution_independent_layout,
    FinalizeReport,
)
from app.services.missions.approval_gate import (
    check_mission_approvable,
    can_approve,
    classify_status,
    is_executable,
    ApprovalReport,
    ApprovalViolation,
)

# ── Smart Executor (ejecución autocurable basada en estado) ──────────
from app.services.missions.state_detector import (
    StateDetector,
    StateSnapshot,
    detect_state,
)
from app.services.missions.execution_contracts import (
    MissionStep,
    StepContract,
    build_contract,
    register_step_kind,
    resolve_url_alias,
)
from app.services.missions.checkpoints import (
    Checkpoint,
    CheckpointManager,
)
from app.services.missions.execution_learning import (
    LearningLogger,
    LearningRecord,
    get_learning_logger,
)
from app.services.missions.action_runner import (
    ActionRunner,
    StrategyResult,
    register_strategy,
)
from app.services.missions.recovery_engine import (
    RecoveryEngine,
    RecoveryPlan,
    register_recipe,
    MAX_PER_STRATEGY,
)
from app.services.missions.smart_executor import (
    SmartMissionExecutor,
    MissionResult,
    StepOutcome,
    run_mission as smart_run_mission,
)
from app.services.missions.semantic_adapter import (
    AdaptedMission,
    AdaptedBlocker,
    adapt_mission_to_steps,
    is_mission_semantic,
)
from app.services.missions.semantic_execution_plan import (
    SEMANTIC_TYPES,
    SemanticExecutionPlan,
    SemanticPlanStep,
    SemanticPlanValidation,
    SemanticPlanViolation,
    StrictValidationCode,
    attach_semantic_plan_to_mission,
    build_semantic_execution_plan,
    has_semantic_plan,
    has_valid_semantic_execution_plan,
    is_generic_profile_label,
    semantic_plan_to_mission_steps,
    validate_semantic_execution_plan_strict,
)
from app.services.missions.smart_runner_bridge import (
    RunnerDecision,
    decide_player,
    run_mission_smart,
)

# ── Auto-Learning Executor ──────────────────────────────────────────
# Imports defensivos: si SQLite no estuviera disponible (raro en CPython
# estándar) o algún componente falla al cargar, no rompemos el paquete.
try:
    from app.services.missions.execution_memory_store import (
        ExecutionMemoryStore,
        RunRecord,
        StepAttemptRecord,
        get_execution_memory_store,
    )
except Exception:  # pragma: no cover
    ExecutionMemoryStore = None  # type: ignore
    RunRecord = None  # type: ignore
    StepAttemptRecord = None  # type: ignore
    get_execution_memory_store = None  # type: ignore

try:
    from app.services.missions.strategy_scoring import (
        StrategyScoringEngine,
        StrategyScore,
        get_strategy_scoring_engine,
    )
except Exception:  # pragma: no cover
    StrategyScoringEngine = None  # type: ignore
    StrategyScore = None  # type: ignore
    get_strategy_scoring_engine = None  # type: ignore

try:
    from app.services.missions.target_memory import (
        TargetMemory,
        TargetMemoryEntry,
        get_target_memory,
    )
except Exception:  # pragma: no cover
    TargetMemory = None  # type: ignore
    TargetMemoryEntry = None  # type: ignore
    get_target_memory = None  # type: ignore

try:
    from app.services.missions.failure_classifier import (
        MissionFailureClassifier,
        MissionFailureType,
        RecoveryRecommendation,
        get_failure_classifier,
    )
except Exception:  # pragma: no cover
    MissionFailureClassifier = None  # type: ignore
    MissionFailureType = None  # type: ignore
    RecoveryRecommendation = None  # type: ignore
    get_failure_classifier = None  # type: ignore

try:
    from app.services.missions.adaptive_timeouts import (
        AdaptiveTimeouts,
        TimeoutRecommendation,
        get_adaptive_timeouts,
        recommended_timeout as adaptive_recommended_timeout,
    )
except Exception:  # pragma: no cover
    AdaptiveTimeouts = None  # type: ignore
    TimeoutRecommendation = None  # type: ignore
    get_adaptive_timeouts = None  # type: ignore
    adaptive_recommended_timeout = None  # type: ignore

try:
    from app.services.missions.machine_profile import (
        MachineProfile,
        MachineProfileSnapshot,
        get_machine_profile,
    )
except Exception:  # pragma: no cover
    MachineProfile = None  # type: ignore
    MachineProfileSnapshot = None  # type: ignore
    get_machine_profile = None  # type: ignore

try:
    from app.services.missions.execution_quality import (
        ExecutionQualityCalculator,
        ExecutionQualityReport,
        compute_execution_quality,
    )
except Exception:  # pragma: no cover
    ExecutionQualityCalculator = None  # type: ignore
    ExecutionQualityReport = None  # type: ignore
    compute_execution_quality = None  # type: ignore

try:
    from app.services.missions.mission_graph_repair import (
        MissionGraphRepair,
        MissionGraphRepairSuggestion,
        get_mission_graph_repair,
    )
except Exception:  # pragma: no cover
    MissionGraphRepair = None  # type: ignore
    MissionGraphRepairSuggestion = None  # type: ignore
    get_mission_graph_repair = None  # type: ignore

try:
    from app.services.missions.auto_learning_executor import (
        AutoLearningExecutor,
        AutoLearningResult,
        run_mission_with_learning,
    )
except Exception:  # pragma: no cover
    AutoLearningExecutor = None  # type: ignore
    AutoLearningResult = None  # type: ignore
    run_mission_with_learning = None  # type: ignore

# ── V2 pipeline (Recorder + Compiler V2) ─────────────────────────────
# Imports defensivos: si alguno fallara (típicamente por dependencia
# opcional), no rompemos el resto del paquete.
try:
    from app.services.missions.raw_event_sanitizer import (
        SanitizeReport,
        sanitize_raw_trace,
        sanitize_mission_in_place,
    )
except Exception:  # pragma: no cover
    SanitizeReport = None  # type: ignore
    sanitize_raw_trace = None  # type: ignore
    sanitize_mission_in_place = None  # type: ignore

try:
    from app.services.missions.field_session_manager import (
        FieldSession,
        build_field_sessions,
    )
except Exception:  # pragma: no cover
    FieldSession = None  # type: ignore
    build_field_sessions = None  # type: ignore

try:
    from app.services.missions.invalid_target_resolver import (
        rescue_compiled_step,
        rescue_steps,
        is_invalid_compiled_step,
    )
except Exception:  # pragma: no cover
    rescue_compiled_step = None  # type: ignore
    rescue_steps = None  # type: ignore
    is_invalid_compiled_step = None  # type: ignore

try:
    from app.services.missions.wait_manager import (
        wait_until,
        WaitResult,
        chrome_open,
        youtube_loaded,
        url_contains,
        suggest_wait_for,
    )
except Exception:  # pragma: no cover
    wait_until = None  # type: ignore
    WaitResult = None  # type: ignore
    chrome_open = None  # type: ignore
    youtube_loaded = None  # type: ignore
    url_contains = None  # type: ignore
    suggest_wait_for = None  # type: ignore

try:
    from app.services.missions.ghost_simulation import (
        simulate_mission,
        mark_missing_for_review,
        GhostReport,
        GhostStepReport,
        GhostStatus,
    )
except Exception:  # pragma: no cover
    simulate_mission = None  # type: ignore
    mark_missing_for_review = None  # type: ignore
    GhostReport = None  # type: ignore
    GhostStepReport = None  # type: ignore
    GhostStatus = None  # type: ignore

try:
    from app.services.missions.execution_confidence import (
        EXEC_AMBER_THRESHOLD,
        EXEC_GREEN_THRESHOLD,
        ExecLevel,
        ExecStrategy,
        ExecutionConfidenceResult,
        MissionExecutionReport,
        compute_execution_confidence,
        compute_mission_execution_confidence,
        execution_confidence_color,
    )
except Exception:  # pragma: no cover
    EXEC_AMBER_THRESHOLD = None  # type: ignore
    EXEC_GREEN_THRESHOLD = None  # type: ignore
    ExecLevel = None  # type: ignore
    ExecStrategy = None  # type: ignore
    ExecutionConfidenceResult = None  # type: ignore
    MissionExecutionReport = None  # type: ignore
    compute_execution_confidence = None  # type: ignore
    compute_mission_execution_confidence = None  # type: ignore
    execution_confidence_color = None  # type: ignore

try:
    from app.services.missions.intent_compiler_v2 import (
        compile_intent,
        apply_intent_to_mission,
        IntentBlock,
        IntentStep,
    )
except Exception:  # pragma: no cover
    compile_intent = None  # type: ignore
    apply_intent_to_mission = None  # type: ignore
    IntentBlock = None  # type: ignore
    IntentStep = None  # type: ignore

try:
    from app.services.missions.intent_collapse_engine import (
        collapse_intent,
        apply_collapse_to_mission,
        plan_to_compiled_steps,
        hard_post_pass,
        CollapsedStep,
        CollapsedBlock,
        RepairQuestion,
        CollapseStatus,
        MissionIntentPlan,
    )
except Exception:  # pragma: no cover
    collapse_intent = None  # type: ignore
    apply_collapse_to_mission = None  # type: ignore
    plan_to_compiled_steps = None  # type: ignore
    hard_post_pass = None  # type: ignore
    CollapsedStep = None  # type: ignore
    CollapsedBlock = None  # type: ignore
    RepairQuestion = None  # type: ignore
    CollapseStatus = None  # type: ignore
    MissionIntentPlan = None  # type: ignore

__all__ = [
    "mission_store",
    "MissionStore",
    "start_recording",
    "stop_recording",
    "stop_and_compile",
    "pause_recording",
    "resume_recording",
    "get_recorder",
    "MissionRecorder",
    "replay_mission",
    "MissionPlayer",
    "TargetResolver",
    "TargetResolutionResult",
    "OneClickCapture",
    "capture_target_bundle",
    "apply_repair_to_step",
    "record_method_outcome",
    "interpret_mission",
    "capture_visual_target",
    "select_target_bbox",
    "compute_asset_bbox",
    "CapturedVisual",
    "TargetCandidate",
    "apply_semantic_fusion",
    "confirm_step_label",
    "finalize_mission_assets",
    "has_temp_paths",
    "enrich_resolution_independent_layout",
    "FinalizeReport",
    "check_mission_approvable",
    "can_approve",
    "classify_status",
    "is_executable",
    "ApprovalReport",
    "ApprovalViolation",
    # Smart Executor
    "StateDetector",
    "StateSnapshot",
    "detect_state",
    "MissionStep",
    "StepContract",
    "build_contract",
    "register_step_kind",
    "resolve_url_alias",
    "Checkpoint",
    "CheckpointManager",
    "LearningLogger",
    "LearningRecord",
    "get_learning_logger",
    "ActionRunner",
    "StrategyResult",
    "register_strategy",
    "RecoveryEngine",
    "RecoveryPlan",
    "register_recipe",
    "MAX_PER_STRATEGY",
    "SmartMissionExecutor",
    "MissionResult",
    "StepOutcome",
    "smart_run_mission",
    # Semantic adapter
    "AdaptedMission",
    "AdaptedBlocker",
    "adapt_mission_to_steps",
    "is_mission_semantic",
    # Semantic Execution Plan (PRD 2026-05-06c)
    "SemanticExecutionPlan",
    "SemanticPlanStep",
    "SEMANTIC_TYPES",
    "SemanticPlanValidation",
    "SemanticPlanViolation",
    "StrictValidationCode",
    "attach_semantic_plan_to_mission",
    "build_semantic_execution_plan",
    "has_semantic_plan",
    "has_valid_semantic_execution_plan",
    "validate_semantic_execution_plan_strict",
    "is_generic_profile_label",
    "semantic_plan_to_mission_steps",
    # Smart runner bridge
    "RunnerDecision",
    "decide_player",
    "run_mission_smart",
    # V2 pipeline
    "SanitizeReport",
    "sanitize_raw_trace",
    "sanitize_mission_in_place",
    "FieldSession",
    "build_field_sessions",
    "rescue_compiled_step",
    "rescue_steps",
    "is_invalid_compiled_step",
    "wait_until",
    "WaitResult",
    "chrome_open",
    "youtube_loaded",
    "url_contains",
    "suggest_wait_for",
    "simulate_mission",
    "mark_missing_for_review",
    "GhostReport",
    "GhostStepReport",
    "GhostStatus",
    # Execution Confidence (PRD 2026-05-06b)
    "EXEC_AMBER_THRESHOLD",
    "EXEC_GREEN_THRESHOLD",
    "ExecLevel",
    "ExecStrategy",
    "ExecutionConfidenceResult",
    "MissionExecutionReport",
    "compute_execution_confidence",
    "compute_mission_execution_confidence",
    "execution_confidence_color",
    "compile_intent",
    "apply_intent_to_mission",
    "IntentBlock",
    "IntentStep",
    # Intent Collapse Engine
    "collapse_intent",
    "apply_collapse_to_mission",
    "plan_to_compiled_steps",
    "hard_post_pass",
    "CollapsedStep",
    "CollapsedBlock",
    "RepairQuestion",
    "CollapseStatus",
    "MissionIntentPlan",
]
