"""FASE 0 — Convergencia brutal: un solo perfil Nevlan en producción.

Objetivo: eliminar perfiles paralelos compitiendo (Nevlan A vs Nevlan B).
En ``ENV=prod`` los flags de runtime convergente son fijos; variables de
entorno del usuario no pueden activar variantes alternativas.

Criterios cubiertos:
  * 0.1 — perfil runtime único en producción
  * Gate FASE 1 — snapshot de flags activos por run (detección de drift)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.services.runtime.beta_runtime_acceptance import enable_beta_runtime_profile

# ARSS OFF en prod — decisión intencional FASE 0 (ver docs/fase0_legacy_route_matrix.md).
ARSS_DISABLED_IN_PROD_RATIONALE = (
    "ARSS (Adaptive Runtime Strategy Synthesis) permanece OFF en producción durante "
    "FASE 0–1 porque: (1) sintetiza variantes mecánicas post-ORCE que compiten con "
    "el perfil único convergido; (2) requiere validación ORCE estable en staging "
    "antes de prod; (3) opt-in explícito (--arss / staging) hasta execute-always ≥95%."
)

FORBIDDEN_PRIMARY_STRATEGY_MARKERS: Sequence[str] = (
    "smart_route",
    "relative_coords",
    "absolute_coords",
    "vision_coords",
    ":coords",
    "coords_emergency",
)

REPRESENTATIVE_GATE_MODES: frozenset = frozenset({"dry-run", "live"})

# Flags que definen "qué Nevlan" está activo. Cualquier divergencia entre
# runs con distintos valores indica perfiles paralelos compitiendo.
CONVERGENCE_FLAG_KEYS: Sequence[str] = (
    "BETA_PRIVATE_RUNTIME_PROFILE",
    "BETA_PRIVATE_RELIABILITY_PROFILE",
    "PCOF_ENABLED",
    "OSUE_ENABLED",
    "OSUE_SHADOW_ENABLED",
    "ARSS_ENABLED",
    "UOOL_ENABLED",
    "OCRP_ENABLED",
    "LONE_LIVE_BRIDGE_ENABLED",
    "ONE_SHOT_RELIABILITY_ENABLED",
    "RELIABILITY_HARDENING_ENABLED",
)

# Valores canónicos esperados en producción (post-convergencia).
PRODUCTION_CONVERGENCE_CANONICAL: Dict[str, bool] = {
    "BETA_PRIVATE_RUNTIME_PROFILE": True,
    "BETA_PRIVATE_RELIABILITY_PROFILE": True,
    "PCOF_ENABLED": True,
    "OSUE_ENABLED": True,
    "OSUE_SHADOW_ENABLED": True,
    "ARSS_ENABLED": False,
    "UOOL_ENABLED": True,
    "OCRP_ENABLED": True,
    "LONE_LIVE_BRIDGE_ENABLED": True,
    "ONE_SHOT_RELIABILITY_ENABLED": True,
    "RELIABILITY_HARDENING_ENABLED": True,
}


def apply_production_convergence_profile(settings: Any) -> Any:
    """Aplica el único perfil Nevlan en producción.

    Ignora toggles de usuario para ``BETA_PRIVATE_*``, ``PCOF``, ``OSUE``,
    ``ARSS`` y el stack runtime asociado. ``ARSS`` permanece OFF en prod.
    """
    enable_beta_runtime_profile(settings, arss_enabled=False)
    settings.ARSS_ENABLED = False
    return settings


def capture_runtime_flag_snapshot(settings: Any) -> Dict[str, bool]:
    """Snapshot serializable de flags convergencia — para reportes y drift."""
    out: Dict[str, bool] = {}
    for key in CONVERGENCE_FLAG_KEYS:
        val = getattr(settings, key, None)
        out[key] = bool(val) if val is not None else False
    return out


def detect_profile_drift(
    snapshot: Dict[str, bool],
    *,
    canonical: Optional[Dict[str, bool]] = None,
) -> List[str]:
    """Devuelve lista de flags que difieren del perfil canónico esperado."""
    expected = canonical or PRODUCTION_CONVERGENCE_CANONICAL
    drift: List[str] = []
    for key, want in expected.items():
        got = bool(snapshot.get(key, False))
        if got != bool(want):
            drift.append(f"{key}: got={got} want={want}")
    return drift


def convergence_profile_label(snapshot: Dict[str, bool]) -> str:
    """Etiqueta legible para detectar Nevlan A vs Nevlan B en reportes."""
    if not snapshot.get("BETA_PRIVATE_RUNTIME_PROFILE"):
        return "legacy_default"
    if snapshot.get("ARSS_ENABLED"):
        return "beta_runtime_arss"
    if snapshot.get("OSUE_ENABLED") and snapshot.get("PCOF_ENABLED"):
        return "beta_runtime_converged"
    if snapshot.get("BETA_PRIVATE_RELIABILITY_PROFILE"):
        return "beta_reliability_only"
    return "beta_partial"


def is_forbidden_primary_strategy(strategy: str) -> bool:
    """True si la estrategia es smart_route/coords como primaria."""
    sl = (strategy or "").lower()
    return any(m in sl for m in FORBIDDEN_PRIMARY_STRATEGY_MARKERS)


def fase0_gate_violations(
    report: Mapping[str, Any],
    *,
    audit_jsonl: Path,
    run_id: str = "",
    require_representative_mode: bool = True,
) -> List[str]:
    """Condiciones que hacen fallar ``make fase0-gate``."""
    violations: List[str] = []

    drift = list(report.get("profile_drift") or [])
    if drift:
        violations.append(f"profile_drift:{';'.join(drift[:4])}")

    if report.get("executive_source") != "semantic_execution_plan":
        violations.append(
            f"executive_source:{report.get('executive_source')!r}",
        )

    if report.get("compiled_graph_executed"):
        violations.append("compiled_graph_executed_with_sep")

    if report.get("coords_used"):
        violations.append("coords_used")

    steps = list(report.get("step_strategies") or [])
    if not steps:
        violations.append("missing_step_strategies")

    for step in steps:
        strat = str(step.get("strategy_used") or "")
        if is_forbidden_primary_strategy(strat):
            violations.append(
                f"forbidden_primary:{step.get('step_id')}:{strat}",
            )

    mode = str(report.get("mode") or "")
    if require_representative_mode and mode not in REPRESENTATIVE_GATE_MODES:
        violations.append(f"non_representative_mode:{mode or 'unknown'}")

    if not audit_jsonl.is_file():
        violations.append("missing_execution_run_audit_jsonl")
    elif run_id:
        try:
            if run_id not in audit_jsonl.read_text(encoding="utf-8"):
                violations.append("audit_jsonl_missing_run_id")
        except Exception:
            violations.append("audit_jsonl_unreadable")

    return violations


def enforce_single_executive_source(mission: Any) -> str:
    """FASE 0.2 — SEP manda; compiled_execution_graph = debug only.

    Marca ``legacy_compiled_graph_purpose`` y devuelve la fuente ejecutiva
    esperada para routing.
    """
    sep = getattr(mission, "semantic_execution_plan", None)
    has_sep = isinstance(sep, dict) and bool(sep.get("steps"))
    if has_sep:
        try:
            mission.legacy_compiled_graph_purpose = "legacy_debug_only"
        except Exception:
            pass
        return "semantic_execution_plan"
    return "none"


__all__ = [
    "ARSS_DISABLED_IN_PROD_RATIONALE",
    "CONVERGENCE_FLAG_KEYS",
    "FORBIDDEN_PRIMARY_STRATEGY_MARKERS",
    "PRODUCTION_CONVERGENCE_CANONICAL",
    "REPRESENTATIVE_GATE_MODES",
    "apply_production_convergence_profile",
    "capture_runtime_flag_snapshot",
    "convergence_profile_label",
    "detect_profile_drift",
    "enforce_single_executive_source",
    "fase0_gate_violations",
    "is_forbidden_primary_strategy",
]
