"""ArthurOS - Config

Objetivo:
- Cargar configuración desde variables de entorno y/o archivo .env en la raíz del proyecto.
- Validar valores críticos (especialmente en producción).
- Centralizar rutas (var/logs, var/sandbox, etc.) y garantizar que existan.

Requisitos:
- pydantic
- pydantic-settings
- python-dotenv (opcional, recomendado)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore


def _find_project_root(start: Path) -> Path:
    """Busca la raíz del proyecto caminando hacia arriba."""
    start = start.resolve()
    for p in [start, *start.parents]:
        if (p / "pyproject.toml").exists() or (p / ".env.example").exists():
            return p
    # fallback razonable para: <root>/app/core/config.py
    return start.parents[2]


PROJECT_ROOT: Path = _find_project_root(Path(__file__).parent)
DOTENV_PATH: Path = PROJECT_ROOT / ".env"

# Cargar .env para librerías externas (opcional). BaseSettings también leerá env_file.
if load_dotenv is not None and DOTENV_PATH.exists():
    load_dotenv(DOTENV_PATH, override=False)


class Settings(BaseSettings):
    """Configuración global validada y tipada."""

    model_config = SettingsConfigDict(
        env_file=str(DOTENV_PATH) if DOTENV_PATH.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- GENERAL ---
    APP_NAME: str = "ArthurOS"
    ENV: Literal["dev", "prod", "test"] = Field(default="dev", description="dev, prod, test")
    DEBUG: bool = False

    # --- RUTAS ---
    BASE_PATH: Path = Field(default=PROJECT_ROOT)
    VAR_PATH: Path = Field(default=PROJECT_ROOT / "var")

    # Subcarpetas estándar
    LOGS_PATH: Path = Field(default=PROJECT_ROOT / "var" / "logs")
    AUDIT_PATH: Path = Field(default=PROJECT_ROOT / "var" / "audit")
    MEMORY_DB_PATH: Path = Field(default=PROJECT_ROOT / "var" / "memory_db")
    SANDBOX_PATH: Path = Field(default=PROJECT_ROOT / "var" / "sandbox")
    CACHE_PATH: Path = Field(default=PROJECT_ROOT / "var" / "cache")

    # --- LLM KEYS (opcionales) ---
    GROQ_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None

    # --- SECURITY ---
    SECRET_KEY: str = Field(default="change_me_in_prod", min_length=8)

    # Semantic-first recorder (shadow): hipótesis en vivo sin cambiar ejecución/SIPE.
    SEMANTIC_FIRST_SHADOW_ENABLED: bool = Field(
        default=True,
        description="Si True, el recorder adjunta intent_timeline en modo shadow.",
    )

    SEMANTIC_SESSION_SHADOW_ENABLED: bool = Field(
        default=True,
        description="Si True, grabación activa session_shadow_mode y Semantic Session Engine.",
    )

    SEMANTIC_COB_SHADOW_ENABLED: bool = Field(
        default=True,
        description="Si True, grabación activa cob_shadow_mode y Canonical Operational Blocks (sombra).",
    )

    TEL_SHADOW_ENABLED: bool = Field(
        default=True,
        description=(
            "Si True, grabación activa tel_shadow_mode y Truth Extraction Layer "
            "(sombra vs SEP)."
        ),
    )

    # TEL → SEP hybrid promotion (Fase 2). Default OFF: no cambia SEP salvo modo primary.
    TEL_SEP_PROMOTION_ENABLED: bool = Field(
        default=False,
        description="Si True, evalúa y opcionalmente aplica SEP derivado desde TEL.",
    )
    TEL_SEP_PROMOTION_MODE: Literal["shadow", "suggest", "primary"] = Field(
        default="shadow",
        description="shadow: solo auditoría+candidato; suggest: auditoría recomienda; "
        "primary: puede reemplazar SEP si READY.",
    )
    TEL_SEP_PROMOTION_MIN_CONFIDENCE: float = Field(
        default=0.68,
        ge=0.0,
        le=1.0,
        description="Confianza mínima promedio del plan TEL para promoción primary.",
    )
    TEL_SEP_PROMOTION_REQUIRE_READY: bool = Field(
        default=True,
        description="Exige validate_tel_sep_ready antes de aplicar primary.",
    )
    TEL_SEP_MAX_AMBIGUITY_AGGREGATE: float = Field(
        default=0.52,
        ge=0.0,
        le=1.0,
        description="Umbral alto de ambiguity_score promedio en truth_blocks.",
    )
    TEL_SEP_AMBIGUITY_GATE_ENABLED: bool = Field(
        default=True,
        description=(
            "Si True, SemanticAmbiguityEngine bloquea ready del candidato cuando "
            "``requires_human_confirmation`` agregado."
        ),
    )

    # COB Execution Pilot — Fase 1 (opcional). Default OFF: sin cambiar runtime SEP legacy.
    COB_EXECUTION_PILOT_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, permite un prefijo opcional ejecutado por el piloto COB "
            "antes del Smart Executor (fallback automático)."
        ),
    )
    COB_EXECUTION_PILOT_SAFE_ONLY: bool = Field(
        default=True,
        description=(
            "Restringe el piloto a acciones whitelist (launcher, URL, nueva pestaña, "
            "búsqueda, scroll de resultados)."
        ),
    )
    COB_EXECUTION_MAX_STEPS: int = Field(
        default=24,
        ge=1,
        le=512,
        description="Techo de micro-operaciones UIC emitidas por el piloto por misión.",
    )
    COB_EXECUTION_REQUIRE_DRY_RUN: bool = Field(
        default=True,
        description="Si True, el piloto exige cob_dry_run_shadow con todas las filas soportadas del COB.",
    )

    # Adaptive Runtime Layer — Fase 1 (opcional). Default OFF: capa paralela SAFE, sin reemplazo de runtime.
    ARL_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, activa Adaptive Runtime Layer (auditoría COB, recuperación SEP opcional, hooks SAFE)."
        ),
    )
    ARL_MAX_RETRIES: int = Field(
        default=1,
        ge=0,
        le=32,
        description="Presupuesto lógico de reintentos/relocalización considerado por decide_continue_vs_fallback.",
    )
    ARL_SEP_RECOVERY_MAX_ROUNDS: int = Field(
        default=1,
        ge=0,
        le=8,
        description=(
            "Por paso SEP: cuántas rondas extra (tras agotar estrategias) permite el hook ARL "
            "antes de RecoveryEngine. 0 desactiva sólo el hook SmartExecutor (COB audit puede seguir)."
        ),
    )

    # Strategy Learning Layer — Fase 1 (opcional). Default OFF: ranking histórico sin contexto rico.
    SLL_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, prioriza estrategias SEP/resolver con evidencia operacional local "
            "(SQLite) combinable con LearningLogger y señales execution_truth/ARL."
        ),
    )
    SLL_MIN_ATTEMPTS: int = Field(
        default=5,
        ge=1,
        le=10_000,
        description="Mínimo de intentos registrados antes de dejar que SLL mueva fuertemente el ranking.",
    )
    SLL_CONFIDENCE_THRESHOLD: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Por debajo de esta confianza, el score aprendido se mezcla hacia neutro.",
    )
    SLL_MAX_PROFILE_SIZE: int = Field(
        default=5000,
        ge=32,
        le=500_000,
        description="Techo de perfiles en SQLite; los más antiguos se podan al insertar.",
    )
    SLL_DECAY_ENABLED: bool = Field(
        default=False,
        description="Si True, aplica decaimiento suave a contadores al reabrir evidencia tras días sin uso.",
    )

    # Adaptive Runtime Strategy Synthesis — síntesis de mecánicas compatibles.
    ARSS_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, sintetiza variantes mecánicas tras divergencias ORCE, "
            "valida con ORCE y promueve solo variantes estables."
        ),
    )

    # Operational Runtime Graph — Fase 1 (sombra; no sustituye SmartExecutor / replay legacy).
    ORG_SHADOW_ENABLED: bool = Field(
        default=True,
        description=(
            "Si True, Mission Review (experto) puede construir/org_audit Operational Runtime Graph sin "
            "tocar SEP ni ejecutores."
        ),
    )

    # Operational Continuity Runtime — Fase 1 (sombra; derivado ORG/TEL/truth — no ejecuta goals reales).
    OCR_SHADOW_ENABLED: bool = Field(
        default=True,
        description=(
            "Si True, Mission Review puede evaluar continuidad operacional (OCR) en paralelo al runtime "
            "step-oriented, sin mutar SEP ni SmartExecutor."
        ),
    )

    # Goal-Oriented Operational Runtime — Fase 1 (sombra; decide por objetivos declarativos; no ejecuta input).
    GOOR_SHADOW_ENABLED: bool = Field(
        default=True,
        description=(
            "Si True y la misión tiene goor_shadow_mode, Mission Review puede evaluar GOOR "
            "(transiciones objetivo-rankeadas) sin alterar ejecutor procedural."
        ),
    )

    # Operational State Understanding Engine — comprensión de estado operacional real.
    OSUE_SHADOW_ENABLED: bool = Field(
        default=True,
        description=(
            "Si True y la misión tiene osue_shadow_mode, captura OperationalStateSnapshot "
            "sin mutar SEP ni SmartExecutor."
        ),
    )
    OSUE_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, OSUE puede bloquear ejecución incompatible, esperas dinámicas "
            "y síntesis ARSS durante estados inestables."
        ),
    )

    # Live Operational Perception — Fase 1 (sombra; observa y recomienda; no ejecuta ni muta SEP).
    LOP_SHADOW_ENABLED: bool = Field(
        default=True,
        description=(
            "Si True y la misión tiene lop_shadow_mode, Mission Review / servicios pueden capturar "
            "LiveOperationalPerceptionSnapshot (UIA/DOM/OCR ligero) sin tocar SmartExecutor ni SEP."
        ),
    )
    # LOP Fase 2 — SmartRunner sólo audita muestras before/after (no decide ejecución).
    LOP_RUNNER_SHADOW_ENABLED: bool = Field(
        default=True,
        description=(
            "Si True, ``lop_live_feed_enabled`` + ``lop_shadow_mode`` activan muestreo read-only "
            "en callbacks de step del smart_runner_bridge."
        ),
    )

    # Pre-Click Operational Freeze — congela identidad operacional antes de mouse_up (default OFF).
    PCOF_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, el recorder congela entidad/colección/UIA/OCR/DOM en mouse_down y "
            "consolida el snapshot antes de transición de UI. post_action_state solo valida outcome."
        ),
    )

    # Operational Memory & Pattern Consolidation — Fase 1 (default OFF).
    OMPC_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, consolida patrones de workflow (COB+SEP) en SQLite local y "
            "expone hints/predicciones deterministas (sin auto-execución agresiva)."
        ),
    )
    OMPC_MIN_PATTERN_OCCURRENCES: int = Field(
        default=2,
        ge=1,
        le=10_000,
        description="Mínimo de ocurrencias antes de considerar el patrón estadísticamente útil.",
    )
    OMPC_CONFIDENCE_THRESHOLD: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Confianza mínima para aplicar boosts OMPC al ranking SLL.",
    )
    OMPC_MAX_PATTERN_MEMORY: int = Field(
        default=3000,
        ge=32,
        le=200_000,
        description="Techo de perfiles de patrón en SQLite.",
    )
    OMPC_TEMPORAL_DECAY_ENABLED: bool = Field(
        default=False,
        description="Si True, decae suavemente recurrence_* al reabrir patrón tras inactividad.",
    )

    # One-Shot Reliability Closure — integración sin capas nuevas (default OFF).
    ONE_SHOT_RELIABILITY_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, activa preflight + auditoría consolidada one-shot antes/después "
            "del SmartRunner (no cambia rutas si está False)."
        ),
    )
    ONE_SHOT_PREFLIGHT_REQUIRED: bool = Field(
        default=True,
        description="Si True, UNSAFE_STOP / NEEDS_HUMAN bloquean ejecución con informe one-shot.",
    )
    ONE_SHOT_USE_LOP: bool = Field(
        default=True,
        description="Si True, intenta refresh LOP en preflight cuando la misión lo permite.",
    )
    ONE_SHOT_ALLOW_COB_PILOT: bool = Field(
        default=False,
        description=(
            "Con one-shot activo: exige este flag además de COB_EXECUTION_PILOT_ENABLED "
            "para lanzar el piloto COB."
        ),
    )
    ONE_SHOT_ALLOW_TEL_SEP_PRIMARY: bool = Field(
        default=False,
        description=(
            "Si False y one-shot activo, la clasificación de plan_source no preferirá "
            "TEL_SEP promovido como primario (auditoría/runtime_mode)."
        ),
    )
    ONE_SHOT_REQUIRE_SAFE_TO_CONTINUE: bool = Field(
        default=True,
        description=(
            "Si True, preflight exige LOP safe_to_continue cuando hay snapshot LOP "
            "con bloqueos operativos."
        ),
    )

    # Reliability hardening program — sin nuevas capas cognitivas (helpers + informes).
    RELIABILITY_HARDENING_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, el SmartRunner puede persistir mission_health_snapshot y "
            "activar utilidades de hardening acotadas."
        ),
    )
    RUNTIME_LATENCY_REPORT_ENABLED: bool = Field(
        default=False,
        description="Si True, genera runtime_latency_report por ejecución smart_runner.",
    )
    PRODUCTION_TELEMETRY_ENABLED: bool = Field(
        default=False,
        description="Si True, acumula runtime_production_telemetry en la Mission durante runs.",
    )
    MAGIC_UX_SANITIZE_RUNNER_STATUS: bool = Field(
        default=False,
        description=(
            "Si True, sanea mensajes de on_status del runner en modo no experto "
            "(tokens técnicos)."
        ),
    )

    # LONE Runtime Bridge — navegación viva LOP + ActionRunner (default OFF).
    LONE_LIVE_BRIDGE_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True y la misión tiene lop_live_feed_enabled, LONE usa percepción LOP "
            "y ActionRunner para scroll/wait/paginate entity-first."
        ),
    )
    LONE_MAX_LIVE_DEPTH: int = Field(
        default=5,
        ge=1,
        le=24,
        description="Profundidad máxima del loop LONE live por recuperación de entidad.",
    )
    LONE_ACTION_SETTLE_MS: int = Field(
        default=300,
        ge=0,
        le=10_000,
        description="Espera acotada tras cada acción de navegación LONE antes de re-observar.",
    )
    LONE_REQUIRE_LOP_SAFE: bool = Field(
        default=True,
        description="Si True, exige LOP safe_to_continue y confianza mínima antes de navegar.",
    )
    LONE_ALLOW_BROWSER_HISTORY: bool = Field(
        default=False,
        description="Permite navigate_back/forward vía historial del navegador (solo si context-safe).",
    )
    LONE_LOOP_BUDGET_MS: int = Field(
        default=8000,
        ge=500,
        le=120_000,
        description="Presupuesto de tiempo máximo del loop LONE por recuperación de entidad.",
    )

    # Entity-first runtime — presupuestos anti-cuelgue (ERL/UCES/LONE).
    ENTITY_RUNTIME_TIMEOUT_MS: int = Field(
        default=12_000,
        ge=1000,
        le=120_000,
        description="Timeout duro acumulado para resolución entity-first por step.",
    )
    ENTITY_MAX_RESOLUTION_ATTEMPTS: int = Field(
        default=3,
        ge=1,
        le=12,
        description="Intentos máximos de resolución entity-first antes de needs_human.",
    )
    ENTITY_UIA_WALK_BUDGET_MS: int = Field(
        default=2500,
        ge=200,
        le=30_000,
        description="Tiempo máximo para UIA WalkControl en collect_runtime_candidates.",
    )
    SMART_EXECUTOR_STEP_TIMEOUT_MS: int = Field(
        default=90_000,
        ge=5000,
        le=600_000,
        description="Timeout global por step en SmartExecutor.",
    )
    ENTITY_STEP_HARD_TIMEOUT_MS: int = Field(
        default=45_000,
        ge=3000,
        le=300_000,
        description="Timeout más estricto para steps select_entity/select_profile.",
    )
    BETA_RUN_TIMEOUT_SEC: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="Timeout por run en scripts/run_beta_acceptance.py.",
    )

    # Unified Operational Orchestration Layer (UOOL) — coordinador único (default OFF).
    UOOL_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, activa UOOL como brain coordinador sobre capas existentes "
            "(no sustituye SmartExecutor)."
        ),
    )
    UOOL_COORDINATE_SMART_EXECUTOR: bool = Field(
        default=True,
        description="Si True, SmartExecutor consulta UOOL antes de hooks autónomos por step.",
    )
    UOOL_PREFLIGHT_REQUIRED: bool = Field(
        default=False,
        description="Si True, preflight UOOL unsafe/human bloquea ejecución en smart_runner_bridge.",
    )
    UOOL_MAX_RECOVERY_ROUNDS: int = Field(default=4, ge=0, le=64)
    UOOL_MAX_REPAIR_ROUNDS: int = Field(default=2, ge=0, le=32)
    UOOL_MAX_NAVIGATION_ROUNDS: int = Field(default=8, ge=0, le=128)
    UOOL_REQUIRE_LIVE_PERCEPTION_ALIGN: bool = Field(
        default=True,
        description="Si True, mismatch LOP safe_to_continue activa recovery coordinado.",
    )
    UOOL_WAIT_MS: int = Field(default=800, ge=0, le=15_000)

    # Operational Convergence & Repeatability Program (OCRP) — default OFF.
    OCRP_ENABLED: bool = Field(
        default=False,
        description=(
            "Si True, activa convergencia/repetibilidad operacional: detectores, "
            "estabilización, timing adaptativo y firmas multi-run."
        ),
    )
    OCRP_STABILIZATION_ENABLED: bool = Field(
        default=True,
        description="Si True, OCRP aplica hints de estabilización a decisiones UOOL.",
    )
    OCRP_MAX_ARL_RETRIES: int = Field(default=1, ge=0, le=8)
    OCRP_BLOCK_UNSTABLE_MISSIONS: bool = Field(
        default=False,
        description="Si True, repeatability_score muy bajo bloquea ejecución en bridge.",
    )

    # Perfil runtime beta privada — UOOL + LONE live + LOP + OCRP + hardening.
    BETA_PRIVATE_RUNTIME_PROFILE: bool = Field(
        default=False,
        description=(
            "Si True, activa stack runtime convergente para beta privada "
            "(UOOL, OCRP, LONE live, LOP shadow/runner, reliability hardening)."
        ),
    )

    # --- Private beta Nevlan — perfil reversible en un sólo toggle ---
    BETA_PRIVATE_RELIABILITY_PROFILE: bool = Field(
        default=False,
        description=(
            "Si True, aplica valores de fiabilidad/medición SAFE para beta privada Nevlan "
            "(reversible desactivándolo)."
        ),
    )

    @property
    def is_prod(self) -> bool:
        return self.ENV == "prod"

    @model_validator(mode="after")
    def _validate_and_normalize(self) -> "Settings":
        # Normaliza paths
        self.BASE_PATH = Path(self.BASE_PATH).expanduser().resolve()
        self.VAR_PATH = Path(self.VAR_PATH).expanduser().resolve()

        def _rebase_if_default(p: Path, default_suffix: str) -> Path:
            expected = (self.BASE_PATH / "var" / default_suffix).resolve()
            current = Path(p).expanduser().resolve()
            if current == expected:
                return (self.VAR_PATH / default_suffix).resolve()
            return current

        self.LOGS_PATH = _rebase_if_default(self.LOGS_PATH, "logs")
        self.AUDIT_PATH = _rebase_if_default(self.AUDIT_PATH, "audit")
        self.MEMORY_DB_PATH = _rebase_if_default(self.MEMORY_DB_PATH, "memory_db")
        self.SANDBOX_PATH = _rebase_if_default(self.SANDBOX_PATH, "sandbox")
        self.CACHE_PATH = _rebase_if_default(self.CACHE_PATH, "cache")

        if self.BETA_PRIVATE_RELIABILITY_PROFILE and not self.is_prod:
            self.ONE_SHOT_RELIABILITY_ENABLED = True
            self.ONE_SHOT_PREFLIGHT_REQUIRED = True
            self.ONE_SHOT_USE_LOP = True
            self.ONE_SHOT_REQUIRE_SAFE_TO_CONTINUE = True
            self.RELIABILITY_HARDENING_ENABLED = True
            self.RUNTIME_LATENCY_REPORT_ENABLED = True
            self.PRODUCTION_TELEMETRY_ENABLED = True
            self.MAGIC_UX_SANITIZE_RUNNER_STATUS = True

            self.ARL_ENABLED = True
            self.ARL_SEP_RECOVERY_MAX_ROUNDS = 1

            self.LOP_SHADOW_ENABLED = True
            self.OSUE_SHADOW_ENABLED = True
            self.OCR_SHADOW_ENABLED = True
            self.GOOR_SHADOW_ENABLED = True
            self.ORG_SHADOW_ENABLED = True
            self.TEL_SHADOW_ENABLED = True

            self.SLL_ENABLED = True
            self.OMPC_ENABLED = True

            self.COB_EXECUTION_PILOT_ENABLED = False
            self.TEL_SEP_PROMOTION_MODE = "suggest"
            self.TEL_SEP_PROMOTION_ENABLED = True

        if self.is_prod or os.getenv("NEVLAN_FORCE_CONVERGENCE_PROFILE", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            # FASE 0 — convergencia brutal: un solo perfil Nevlan.
            # BETA_PRIVATE_*, PCOF, OSUE, ARSS no son toggles de usuario en prod.
            from app.services.runtime.nevlan_runtime_convergence import (
                apply_production_convergence_profile,
            )

            apply_production_convergence_profile(self)
        elif self.BETA_PRIVATE_RUNTIME_PROFILE:
            from app.services.runtime.beta_runtime_acceptance import enable_beta_runtime_profile

            enable_beta_runtime_profile(self, arss_enabled=False)

        # PCOF en dev (no prod — prod usa perfil convergido fijo).
        if self.ENV == "dev" and not self.is_prod:
            self.PCOF_ENABLED = True

        if self.is_prod:
            if self.SECRET_KEY.strip() == "change_me_in_prod":
                raise ValueError("SECRET_KEY debe ser configurada (no puede ser 'change_me_in_prod') en PROD.")
            if self.DEBUG:
                raise ValueError("DEBUG no debe estar activado en PROD.")
        return self


def ensure_runtime_dirs(s: Settings) -> None:
    """Crea carpetas críticas (idempotente)."""
    s.VAR_PATH.mkdir(parents=True, exist_ok=True)
    for p in (s.LOGS_PATH, s.AUDIT_PATH, s.MEMORY_DB_PATH, s.SANDBOX_PATH, s.CACHE_PATH):
        p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def invalidate_settings_cache() -> None:
    """Pruebas o recarga aislada: limpia el singleton cacheado."""

    try:
        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass


try:
    settings = get_settings()
    ensure_runtime_dirs(settings)
except ValidationError as e:
    print(f"❌ Error crítico de configuración: {e}")
    raise
