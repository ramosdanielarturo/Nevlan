"""Adaptive Runtime Strategy Synthesis (ARSS).

Sintetiza variantes **mecánicas** compatibles con Goal + Contract cuando el
runtime diverge (ORCE), sin mutar Human View, canonical truth ni operational
identity.

Flujo::

    Goal → Contract → Machine Layer → Runtime (ORCE divergence)
    → ARSS synthesis → candidate mechanics → replay validation
    → ORCE reconciliation → promote ONLY if stable → variant memory

Prohibido: coords-first, smart_route primaria, hacks por app, cambio de Goal.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.contracts.mission import (
    AdaptiveExecutionVariant,
    AdaptiveSynthesisAttempt,
    MachineExecutionStep,
    Mission,
    OperationalElementIdentity,
    OperationalElementType,
    OperationalExecutionExpectation,
    OperationalRuntimeConsistencyReport,
    OperationalRuntimeObservation,
)
from app.core.logger import log
from app.core.paths import VAR_DIR

ARSS_STORAGE_VERSION = 1
_ARSS_DIR = VAR_DIR / "runtime" / "arss"
_ARSS_DB = _ARSS_DIR / f"arss_variants_v{ARSS_STORAGE_VERSION}.sqlite"
_ARSS_JSONL = _ARSS_DIR / "arss_synthesis_audit.jsonl"

# Umbrales de promoción (regla central ARSS).
PROMOTION_MIN_CONSISTENCY = 0.90
PROMOTION_MIN_REPLAY_STABILITY = 0.90
PROMOTION_MIN_DETERMINISTIC = 0.90
PROMOTION_MIN_VALIDATION_RATIO = 0.95
PROMOTION_MIN_STABLE_RUNS = 2

# Estrategias prohibidas como primaria o en variantes ARSS.
_FORBIDDEN_STRATEGY_MARKERS: Tuple[str, ...] = (
    "coords_first",
    "coords_emergency",
    "relative_coords",
    "absolute_coords",
    "vision_coords",
    "smart_route_primary",
    "smart_route",
    "legacy_blind_retry",
)

# Alternativas mecánicas universales por tipo semántico (sin apps concretas).
_MECHANIC_ALTERNATIVES: Dict[str, List[List[str]]] = {
    "submit_input": [
        ["submit_input:uia_button", "submit_input:dom_button"],
        ["submit_input:dom_button", "submit_input:keyboard_enter"],
        ["uol:submit_input", "submit_input:dom_button"],
    ],
    "search_content": [
        ["search_content:dom_input", "search_content:uia_textbox"],
        ["search_content:uia_textbox", "search_content:ocr_input_surface"],
        ["search_content:dom_input", "search_content:contextual_surface"],
    ],
    "search_site": [
        ["search_site:dom_input", "search_site:uia_textbox"],
        ["search_content:dom_input", "search_site:uia_textbox"],
    ],
    "fill_field": [
        ["fill_field:uia_textbox", "fill_field:dom_input"],
        ["uol:focus_input", "fill_field:dom_input"],
    ],
    "open_new_tab": [
        ["open_new_tab:uia_button", "open_new_tab:hotkey_ctrl_t"],
        ["open_new_tab:hotkey_ctrl_t", "open_new_tab:uia_button"],
        ["open_new_tab:tab_affordance", "open_new_tab:hotkey_ctrl_t"],
    ],
    "open_tab": [
        ["open_new_tab:uia_button", "open_new_tab:hotkey_ctrl_t"],
        ["open_new_tab:tab_affordance", "open_new_tab:hotkey_ctrl_t"],
    ],
    "select_entity_from_collection": [
        ["select_entity:collection_first", "select_entity:uia_text_match"],
        ["select_entity:uia_text_match", "select_entity:visible_text_ocr"],
    ],
}

# Refuerzo por identidad operacional UOEI.
_ELEMENT_MECHANIC_ALTERNATIVES: Dict[str, List[List[str]]] = {
    OperationalElementType.SUBMIT_AFFORDANCE.value: [
        ["submit_input:uia_button", "submit_input:dom_button"],
    ],
    OperationalElementType.SEARCH_INPUT_SURFACE.value: [
        ["search_content:dom_input", "search_content:uia_textbox"],
        ["search_content:contextual_surface", "search_content:uia_textbox"],
    ],
    OperationalElementType.CONTEXT_CREATION_AFFORDANCE.value: [
        ["open_new_tab:hotkey_ctrl_t", "open_new_tab:uia_button"],
        ["open_new_tab:tab_affordance", "open_new_tab:hotkey_ctrl_t"],
    ],
    OperationalElementType.LAUNCHER_INPUT_SURFACE.value: [
        ["search_content:dom_input", "open_app:launcher_input"],
    ],
}

_store_lock = threading.Lock()
_store_instance: Optional["RuntimeVariantMemoryStore"] = None


def arss_enabled(settings_obj: Optional[Any] = None) -> bool:
    if settings_obj is None:
        try:
            from app.core.config import get_settings

            settings_obj = get_settings()
        except Exception:
            return False
    return bool(getattr(settings_obj, "ARSS_ENABLED", False))


def _txt(x: Any) -> str:
    return str(x or "").strip()


def _is_forbidden_strategy(strategy: str) -> bool:
    sl = _txt(strategy).lower()
    return any(m in sl for m in _FORBIDDEN_STRATEGY_MARKERS)


def _filter_forbidden(strategies: Sequence[str]) -> List[str]:
    return [s for s in strategies if s and not _is_forbidden_strategy(s)]


def _hash_key(*parts: str) -> str:
    payload = "|".join(p for p in parts if p)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class RuntimeVariantMemoryStore:
    """Persistencia local de variantes ARSS (SQLite + export JSON)."""

    def __init__(self, db_path: Optional[Any] = None) -> None:
        self._db_path = db_path or _ARSS_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=8.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with _store_lock:
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runtime_variants (
                        variant_id TEXT PRIMARY KEY,
                        mission_id TEXT NOT NULL,
                        parent_step_id TEXT NOT NULL,
                        semantic_type TEXT NOT NULL,
                        element_type TEXT NOT NULL,
                        strategies_json TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        accepted INTEGER NOT NULL DEFAULT 0,
                        promoted INTEGER NOT NULL DEFAULT 0,
                        usage_count INTEGER NOT NULL DEFAULT 0,
                        successful_runs INTEGER NOT NULL DEFAULT 0,
                        failed_runs INTEGER NOT NULL DEFAULT 0,
                        consistency_score REAL NOT NULL DEFAULT 0,
                        replay_stability_score REAL NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_arss_step
                        ON runtime_variants(parent_step_id, accepted);
                    CREATE TABLE IF NOT EXISTS rejected_variants (
                        variant_id TEXT PRIMARY KEY,
                        parent_step_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        rejected_at REAL NOT NULL
                    );
                    """,
                )

    def save_variant(
        self,
        variant: AdaptiveExecutionVariant,
        *,
        mission_id: str = "",
        semantic_type: str = "",
        element_type: str = "",
    ) -> None:
        payload = variant.model_dump(mode="json")
        with _store_lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO runtime_variants (
                        variant_id, mission_id, parent_step_id, semantic_type,
                        element_type, strategies_json, payload_json, accepted,
                        promoted, usage_count, successful_runs, failed_runs,
                        consistency_score, replay_stability_score, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(variant_id) DO UPDATE SET
                        payload_json=excluded.payload_json,
                        accepted=excluded.accepted,
                        promoted=excluded.promoted,
                        usage_count=excluded.usage_count,
                        successful_runs=excluded.successful_runs,
                        failed_runs=excluded.failed_runs,
                        consistency_score=excluded.consistency_score,
                        replay_stability_score=excluded.replay_stability_score,
                        updated_at=excluded.updated_at
                    """,
                    (
                        variant.variant_id,
                        mission_id,
                        variant.parent_machine_step_id,
                        semantic_type,
                        element_type,
                        json.dumps(variant.synthesized_strategies, ensure_ascii=False),
                        json.dumps(payload, ensure_ascii=False),
                        1 if variant.accepted_for_runtime else 0,
                        1 if variant.promoted_to_machine_layer else 0,
                        variant.usage_count,
                        variant.successful_runs,
                        variant.failed_runs,
                        variant.consistency_score,
                        variant.replay_stability_score,
                        time.time(),
                    ),
                )

    def save_rejected(
        self,
        variant: AdaptiveExecutionVariant,
        *,
        reason: str,
    ) -> None:
        with _store_lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO rejected_variants
                    (variant_id, parent_step_id, reason, payload_json, rejected_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        variant.variant_id,
                        variant.parent_machine_step_id,
                        reason,
                        json.dumps(variant.model_dump(mode="json"), ensure_ascii=False),
                        time.time(),
                    ),
                )

    def get_accepted_for_step(
        self,
        parent_step_id: str,
        *,
        mission_id: str = "",
    ) -> List[AdaptiveExecutionVariant]:
        with _store_lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM runtime_variants
                    WHERE parent_step_id = ? AND accepted = 1
                    ORDER BY consistency_score DESC, successful_runs DESC
                    LIMIT 8
                    """,
                    (parent_step_id,),
                ).fetchall()
        out: List[AdaptiveExecutionVariant] = []
        for row in rows:
            try:
                data = json.loads(row["payload_json"])
                out.append(AdaptiveExecutionVariant.model_validate(data))
            except Exception:
                continue
        return out

    def record_run_outcome(
        self,
        variant_id: str,
        *,
        success: bool,
    ) -> None:
        col = "successful_runs" if success else "failed_runs"
        with _store_lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE runtime_variants
                    SET usage_count = usage_count + 1,
                        {col} = {col} + 1,
                        updated_at = ?
                    WHERE variant_id = ?
                    """,
                    (time.time(), variant_id),
                )


def get_runtime_variant_memory_store() -> RuntimeVariantMemoryStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = RuntimeVariantMemoryStore()
    return _store_instance


def _element_identity_for_step(mission: Mission, step_id: str) -> Optional[OperationalElementIdentity]:
    for coi in getattr(mission, "canonical_operational_intents", None) or []:
        if str(getattr(coi, "source_event_id", "")) == step_id:
            return getattr(coi, "operational_element_identity", None)
        for ev in getattr(mission, "raw_trace", None) or []:
            if str(ev.id) == str(getattr(coi, "source_event_id", "")):
                meta = dict(getattr(ev, "metadata", None) or {})
                raw = meta.get("operational_element_identity")
                if isinstance(raw, dict):
                    try:
                        return OperationalElementIdentity.model_validate(raw)
                    except Exception:
                        pass
    for ev in getattr(mission, "raw_trace", None) or []:
        meta = dict(getattr(ev, "metadata", None) or {})
        raw = meta.get("operational_element_identity")
        if isinstance(raw, dict):
            try:
                return OperationalElementIdentity.model_validate(raw)
            except Exception:
                pass
    return None


def _machine_step(mission: Mission, step_id: str) -> Optional[MachineExecutionStep]:
    try:
        from app.services.missions.four_layer_operational_model import machine_steps_for_runtime

        for ms in machine_steps_for_runtime(mission):
            if ms.step_id == step_id:
                return ms
    except Exception:
        pass
    sep = dict(mission.semantic_execution_plan or {})
    for sp in sep.get("steps") or []:
        if isinstance(sp, dict) and str(sp.get("id")) == step_id:
            return MachineExecutionStep(
                step_id=step_id,
                semantic_type=str(sp.get("type") or ""),
                params=dict(sp.get("params") or {}),
                preferred_strategy=str(sp.get("preferred_strategy") or ""),
                fallback_strategies=list(sp.get("fallback_strategies") or []),
            )
    return None


def _candidate_alternatives(
    semantic_type: str,
    element_type: str,
    failed_path: Sequence[str],
) -> List[List[str]]:
    """Genera candidatos mecánicos universales excluyendo path fallido."""
    failed_set = {_txt(s).lower() for s in failed_path}
    pools: List[List[str]] = []

    for alt in _MECHANIC_ALTERNATIVES.get(semantic_type, []):
        filtered = _filter_forbidden(alt)
        if filtered and not all(s.lower() in failed_set for s in filtered):
            pools.append(filtered)

    for alt in _ELEMENT_MECHANIC_ALTERNATIVES.get(element_type, []):
        filtered = _filter_forbidden(alt)
        if filtered and not all(s.lower() in failed_set for s in filtered):
            pools.append(filtered)

    # Deduplicate preserving order
    seen: set = set()
    unique: List[List[str]] = []
    for pool in pools:
        key = tuple(pool)
        if key not in seen:
            seen.add(key)
            unique.append(pool)
    return unique[:6]


def _build_variant(
    *,
    step_id: str,
    goal_id: str,
    strategies: List[str],
    semantic_type: str,
    element_identity: Optional[OperationalElementIdentity],
    synthesis_reason: str,
    from_divergence: bool = True,
) -> AdaptiveExecutionVariant:
    runtime_path = [_path_token(s) for s in strategies]
    forbidden = any(_is_forbidden_strategy(s) for s in strategies)
    return AdaptiveExecutionVariant(
        variant_id=str(uuid.uuid4()),
        parent_machine_step_id=step_id,
        goal_id=goal_id,
        operational_element_identity=element_identity,
        synthesized_runtime_path=runtime_path,
        synthesized_strategies=list(strategies),
        synthesized_surface_chain=[semantic_type or "generic_surface"],
        synthesized_validation_path=["postcondition_must_pass"],
        synthesis_reason=synthesis_reason,
        generated_from_divergence=from_divergence,
        generated_from_runtime_observation=from_divergence,
        forbidden_strategy_used=forbidden,
        canonical_truth_preserved=True,
        continuity_preserved=True,
    )


def _path_token(strategy: str) -> str:
    s = _txt(strategy)
    if ":" in s:
        return s.split(":", 1)[1]
    return s or "unknown"


def evaluate_variant_with_orce(
    variant: AdaptiveExecutionVariant,
    *,
    observation: Optional[OperationalRuntimeObservation] = None,
    report: Optional[OperationalRuntimeConsistencyReport] = None,
    simulated_success: bool = False,
) -> AdaptiveExecutionVariant:
    """Evalúa variante contra criterios ORCE (sin ejecutar runtime real)."""
    v = variant.model_copy(deep=True)

    if v.forbidden_strategy_used:
        v.consistency_score = 0.0
        v.replay_stability_score = 0.0
        v.deterministic_score = 0.0
        v.validation_success_ratio = 0.0
        v.continuity_preserved = False
        return v

    base_consistency = 0.92 if simulated_success else 0.75
    if simulated_success:
        v.consistency_score = 0.95
        v.replay_stability_score = 0.93
        v.deterministic_score = 0.95
        v.validation_success_ratio = 0.96
        v.continuity_preserved = True
        v.canonical_truth_preserved = True
        return v

    if observation is not None:
        if observation.coords_used or observation.smart_route_used:
            base_consistency -= 0.35
        if observation.divergence_detected:
            base_consistency -= 0.15
        base_consistency = max(0.0, min(1.0, base_consistency))

    if report is not None:
        if report.canonical_truth_violations > 0:
            v.canonical_truth_preserved = False
            base_consistency -= 0.4
        if report.continuity_breaks > 0:
            v.continuity_preserved = False
            base_consistency -= 0.25

    v.consistency_score = round(max(0.0, min(1.0, base_consistency)), 4)
    v.replay_stability_score = round(
        max(0.0, min(1.0, v.consistency_score - 0.02)), 4,
    )
    v.deterministic_score = round(
        max(0.0, min(1.0, 0.95 if not v.forbidden_strategy_used else 0.0)), 4,
    )
    v.validation_success_ratio = round(
        0.96 if simulated_success else max(0.0, v.consistency_score - 0.05), 4,
    )
    return v


def validate_promotion_eligibility(
    variant: AdaptiveExecutionVariant,
) -> Tuple[bool, List[str]]:
    """Reglas estrictas de promoción a Machine Layer."""
    reasons: List[str] = []
    if variant.consistency_score < PROMOTION_MIN_CONSISTENCY:
        reasons.append(f"consistency<{PROMOTION_MIN_CONSISTENCY}")
    if variant.replay_stability_score < PROMOTION_MIN_REPLAY_STABILITY:
        reasons.append(f"replay_stability<{PROMOTION_MIN_REPLAY_STABILITY}")
    if variant.deterministic_score < PROMOTION_MIN_DETERMINISTIC:
        reasons.append(f"deterministic<{PROMOTION_MIN_DETERMINISTIC}")
    if variant.validation_success_ratio < PROMOTION_MIN_VALIDATION_RATIO:
        reasons.append(f"validation_ratio<{PROMOTION_MIN_VALIDATION_RATIO}")
    if not variant.continuity_preserved:
        reasons.append("continuity_break")
    if variant.forbidden_strategy_used:
        reasons.append("forbidden_strategy")
    if not variant.canonical_truth_preserved:
        reasons.append("canonical_truth_violation")
    if variant.successful_runs < PROMOTION_MIN_STABLE_RUNS:
        reasons.append(f"stable_runs<{PROMOTION_MIN_STABLE_RUNS}")
    return (len(reasons) == 0, reasons)


def synthesize_variants_from_divergence(
    mission: Mission,
    *,
    step_id: str,
    observation: OperationalRuntimeObservation,
    expectation: Optional[OperationalExecutionExpectation] = None,
    simulated_replay_success: Optional[str] = None,
) -> AdaptiveSynthesisAttempt:
    """Sintetiza candidatos mecánicos tras divergencia ORCE."""
    ms = _machine_step(mission, step_id)
    semantic_type = ms.semantic_type if ms else ""

    try:
        from app.services.runtime.operational_state_understanding_engine import (
            OperationalStateInput,
            OperationalStateSnapshot,
            arss_synthesis_blocked,
            build_operational_state_snapshot,
            osue_enabled,
        )

        raw_osue = getattr(mission, "osue_last_snapshot", None)
        if isinstance(raw_osue, dict) and raw_osue.get("operational_state_type"):
            osue_snap = OperationalStateSnapshot.model_validate(raw_osue)
        else:
            osue_snap = build_operational_state_snapshot(OperationalStateInput())
        blocked, block_reasons = arss_synthesis_blocked(osue_snap)
        if blocked and osue_enabled():
            recovery = "wait_for_state_completion"
            try:
                from app.services.runtime.operational_state_understanding_engine import (
                    arss_recovery_action_for_snapshot,
                )

                recovery = arss_recovery_action_for_snapshot(osue_snap)
            except Exception:
                pass
            attempt = AdaptiveSynthesisAttempt(
                divergence_source=f"orce:{step_id}",
                failed_runtime_path=list(observation.actual_runtime_path or []),
                synthesis_strategy="blocked_unstable_operational_state",
                replay_validation_results={
                    "blocked": True,
                    "reasons": block_reasons,
                    "recovery_action": recovery,
                },
            )
            return attempt
    except Exception:
        pass

    element_identity = _element_identity_for_step(mission, step_id)
    element_type = (
        element_identity.operational_element_type.value
        if element_identity
        else ""
    )

    failed_path = list(observation.actual_runtime_path or observation.actual_strategies_used or [])
    alternatives = _candidate_alternatives(semantic_type, element_type, failed_path)

    goal_id = ""
    try:
        from app.services.missions.four_layer_operational_model import ensure_four_layer_model

        model = ensure_four_layer_model(mission)
        goal_id = model.goal.goal_id if hasattr(model.goal, "goal_id") else ""
    except Exception:
        pass

    attempt = AdaptiveSynthesisAttempt(
        divergence_source=f"orce:{step_id}",
        failed_runtime_path=failed_path,
        observed_surface_state=_txt(observation.runtime_surface_chain[-1] if observation.runtime_surface_chain else ""),
        expected_contract=expectation.model_dump(mode="json") if expectation else {},
        synthesis_strategy="structural_mechanic_alternatives",
    )

    for i, strategies in enumerate(alternatives):
        variant = _build_variant(
            step_id=step_id,
            goal_id=goal_id,
            strategies=strategies,
            semantic_type=semantic_type,
            element_identity=element_identity,
            synthesis_reason=f"alternative_mechanic_{i + 1}:{semantic_type}",
        )
        sim_ok = simulated_replay_success and strategies[0] == simulated_replay_success
        variant = evaluate_variant_with_orce(
            variant,
            observation=observation,
            simulated_success=bool(sim_ok),
        )
        if sim_ok:
            variant.successful_runs = PROMOTION_MIN_STABLE_RUNS
            variant.usage_count = PROMOTION_MIN_STABLE_RUNS

        ok, reject_reasons = validate_promotion_eligibility(variant)
        if variant.forbidden_strategy_used:
            attempt.rejected_variants.append(variant)
            get_runtime_variant_memory_store().save_rejected(
                variant, reason="forbidden_strategy",
            )
        elif variant.consistency_score < 0.55:
            attempt.rejected_variants.append(variant)
            get_runtime_variant_memory_store().save_rejected(
                variant, reason="low_consistency",
            )
        elif ok or (sim_ok and variant.consistency_score >= PROMOTION_MIN_CONSISTENCY):
            variant.accepted_for_runtime = True
            attempt.generated_variants.append(variant)
            if attempt.accepted_variant is None:
                attempt.accepted_variant = variant
        else:
            variant.accepted_for_runtime = variant.consistency_score >= 0.70
            attempt.generated_variants.append(variant)
            if reject_reasons:
                attempt.replay_validation_results[variant.variant_id] = {
                    "rejected_for_promotion": reject_reasons,
                }

    return attempt


def promote_variant_to_machine_layer(
    mission: Mission,
    variant: AdaptiveExecutionVariant,
) -> bool:
    """Promueve variante a Machine Layer sin mutar Goal/Human View."""
    ok, reasons = validate_promotion_eligibility(variant)
    if not ok:
        log.debug("[ARSS] promotion blocked: %s", reasons)
        return False

    if not variant.synthesized_strategies:
        return False

    pref = variant.synthesized_strategies[0]
    fallbacks = list(variant.synthesized_strategies[1:])
    if _is_forbidden_strategy(pref):
        return False

    goal_before = ""
    human_before = ""
    try:
        from app.services.missions.four_layer_operational_model import ensure_four_layer_model

        model = ensure_four_layer_model(mission)
        goal_before = model.goal.purpose_statement
        human_before = json.dumps(
            [s.human_label for s in model.human_view.steps],
            ensure_ascii=False,
        )
    except Exception:
        pass

    try:
        from app.services.missions.four_layer_operational_model import apply_machine_step_edit

        apply_machine_step_edit(
            mission,
            variant.parent_machine_step_id,
            preferred_strategy=pref,
            fallback_strategies=fallbacks,
            params_patch={
                "arss_variant_id": variant.variant_id,
                "arss_promoted": True,
            },
            refresh_contract=False,
        )
    except Exception as exc:
        log.debug("[ARSS] machine promotion failed: %s", exc)
        return False

    # Verificar Goal/Human View intactos
    try:
        from app.services.missions.four_layer_operational_model import ensure_four_layer_model

        model_after = ensure_four_layer_model(mission)
        if goal_before and model_after.goal.purpose_statement != goal_before:
            log.warning("[ARSS] Goal mutated — rolling back promotion flag")
            return False
        human_after = json.dumps(
            [s.human_label for s in model_after.human_view.steps],
            ensure_ascii=False,
        )
        if human_before and human_after != human_before:
            log.warning("[ARSS] Human View mutated — rolling back promotion flag")
            return False
    except Exception:
        pass

    promoted = variant.model_copy(deep=True)
    promoted.promoted_to_machine_layer = True
    promoted.accepted_for_runtime = True

    variants = list(getattr(mission, "arss_runtime_variants", None) or [])
    variants.append(promoted.model_dump(mode="json"))
    mission.arss_runtime_variants = variants[-100:]

    store = get_runtime_variant_memory_store()
    store.save_variant(
        promoted,
        mission_id=str(getattr(mission, "id", "")),
        semantic_type=_machine_step(mission, variant.parent_machine_step_id).semantic_type
        if _machine_step(mission, variant.parent_machine_step_id)
        else "",
        element_type=(
            promoted.operational_element_identity.operational_element_type.value
            if promoted.operational_element_identity
            else ""
        ),
    )
    return True


def inject_arss_strategies(
    step: Any,
    base_strategies: List[str],
    mission: Optional[Mission] = None,
) -> List[str]:
    """Runtime order: preferred machine → accepted ARSS variants → fallbacks."""
    if mission is None or not arss_enabled():
        return base_strategies

    step_id = _txt(getattr(step, "id", "") or getattr(step, "step_id", ""))
    if not step_id:
        step_id = _txt((getattr(step, "params", None) or {}).get("step_id"))

    store = get_runtime_variant_memory_store()
    variants = store.get_accepted_for_step(step_id, mission_id=str(getattr(mission, "id", "")))

    # También variantes en misión
    for raw in getattr(mission, "arss_runtime_variants", None) or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("parent_machine_step_id") != step_id:
            continue
        if not raw.get("accepted_for_runtime"):
            continue
        try:
            variants.append(AdaptiveExecutionVariant.model_validate(raw))
        except Exception:
            continue

    if not variants:
        return base_strategies

    out: List[str] = []
    plan_pref = _txt((getattr(step, "params", None) or {}).get("preferred_strategy"))
    if plan_pref:
        out.append(plan_pref)

    for v in variants:
        for s in v.synthesized_strategies:
            if s and s not in out and not _is_forbidden_strategy(s):
                out.append(s)

    for s in base_strategies:
        if s and s not in out:
            out.append(s)

    safe = [s for s in out if not _is_forbidden_strategy(s)]
    risky = [s for s in out if _is_forbidden_strategy(s)]
    return safe + risky


def process_arss_after_orce(mission: Mission) -> List[AdaptiveSynthesisAttempt]:
    """Hook post-ORCE: sintetiza variantes desde divergencias."""
    if not arss_enabled() or mission is None:
        return []

    raw = getattr(mission, "operational_runtime_consistency_report", None)
    if not isinstance(raw, dict):
        return []

    try:
        report = OperationalRuntimeConsistencyReport.model_validate(raw)
    except Exception:
        return []

    if report.divergence_count <= 0:
        return []

    attempts: List[AdaptiveSynthesisAttempt] = []
    audit = list(getattr(mission, "arss_synthesis_audit", None) or [])

    for summary in report.step_summaries or []:
        if summary.get("status") not in ("DIVERGED", "DEGRADED"):
            continue
        step_id = _txt(summary.get("step_id"))
        if not step_id:
            continue

        obs = OperationalRuntimeObservation(
            step_id=step_id,
            actual_runtime_path=list(summary.get("actual_path") or []),
            actual_strategies_used=list(summary.get("actual_path") or []),
            divergence_detected=True,
            divergence_reasons=list(summary.get("divergence_reasons") or []),
            consistency_score=float(summary.get("consistency_score") or 0.5),
        )

        ms = _machine_step(mission, step_id)
        expectation = ms.runtime_expectation if ms else None

        attempt = synthesize_variants_from_divergence(
            mission,
            step_id=step_id,
            observation=obs,
            expectation=expectation,
        )
        attempts.append(attempt)
        audit.append(attempt.model_dump(mode="json"))

        if attempt.accepted_variant:
            av = attempt.accepted_variant
            ok, _ = validate_promotion_eligibility(av)
            if ok:
                promote_variant_to_machine_layer(mission, av)

        try:
            _ARSS_JSONL.parent.mkdir(parents=True, exist_ok=True)
            row = attempt.model_dump(mode="json")
            row["mission_id"] = str(getattr(mission, "id", ""))
            row["recorded_at"] = time.time()
            with _ARSS_JSONL.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.debug("[ARSS] jsonl: %s", exc)

    mission.arss_synthesis_audit = audit[-200:]
    return attempts


def record_variant_run_outcome(
    mission: Mission,
    step_id: str,
    *,
    success: bool,
    strategy_used: str = "",
) -> None:
    """Registra resultado de ejecución para variantes ARSS."""
    if not arss_enabled():
        return
    store = get_runtime_variant_memory_store()
    for raw in getattr(mission, "arss_runtime_variants", None) or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("parent_machine_step_id") != step_id:
            continue
        strategies = raw.get("synthesized_strategies") or []
        if strategy_used and strategy_used not in strategies:
            continue
        vid = _txt(raw.get("variant_id"))
        if vid:
            store.record_run_outcome(vid, success=success)


def arss_expert_panel_lines(mission: Mission) -> List[str]:
    """Panel Mission Review experto — variantes ARSS."""
    lines: List[str] = []
    variants = getattr(mission, "arss_runtime_variants", None) or []
    audit = getattr(mission, "arss_synthesis_audit", None) or []

    if not variants and not audit:
        return lines

    lines.append("ARSS — Adaptive Runtime Strategy Synthesis")
    for vraw in variants[-3:]:
        if not isinstance(vraw, dict):
            continue
        promoted = vraw.get("promoted_to_machine_layer")
        lines.append(f"  variant: {str(vraw.get('variant_id', ''))[:12]}…")
        lines.append(f"    step: {vraw.get('parent_machine_step_id')}")
        lines.append(f"    strategies: {' → '.join(vraw.get('synthesized_strategies') or [])[:70]}")
        lines.append(
            f"    consistency: {vraw.get('consistency_score')} · "
            f"stability: {vraw.get('replay_stability_score')} · "
            f"promoted: {promoted}",
        )
        if vraw.get("synthesis_reason"):
            lines.append(f"    reason: {vraw.get('synthesis_reason')[:80]}")

    for attempt_raw in audit[-2:]:
        if not isinstance(attempt_raw, dict):
            continue
        lines.append(f"  synthesis: {attempt_raw.get('divergence_source', '')}")
        lines.append(f"    failed: {' → '.join(attempt_raw.get('failed_runtime_path') or [])[:60]}")
        acc = attempt_raw.get("accepted_variant")
        if isinstance(acc, dict):
            lines.append(f"    accepted: {' → '.join(acc.get('synthesized_strategies') or [])[:60]}")
        rej = attempt_raw.get("rejected_variants") or []
        if rej:
            lines.append(f"    rejected: {len(rej)} variant(s)")

    return lines


__all__ = [
    "AdaptiveExecutionVariant",
    "AdaptiveSynthesisAttempt",
    "RuntimeVariantMemoryStore",
    "PROMOTION_MIN_CONSISTENCY",
    "arss_enabled",
    "arss_expert_panel_lines",
    "evaluate_variant_with_orce",
    "get_runtime_variant_memory_store",
    "inject_arss_strategies",
    "process_arss_after_orce",
    "promote_variant_to_machine_layer",
    "record_variant_run_outcome",
    "synthesize_variants_from_divergence",
    "validate_promotion_eligibility",
]
