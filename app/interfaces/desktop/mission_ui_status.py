"""Estado ejecutable unificado para la UI desktop (Nevlan).

Toda decisión visible sobre «¿lista para ejecutar?» / «¿necesita revisión?»
debe basarse aquí — **no** en ``mission.status`` persistido (salvo estados
de workflow que el SEP no cubre: grabación, interpretación, fallo).

Fuentes:
  * :func:`app.services.missions.mission_truth_gate.gate_decision`
  * :func:`app.services.missions.mission_review_summary.build_mission_review_summary`
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Tuple

from app.contracts.mission import MissionStatus
from app.services.missions.mission_review_summary import build_mission_review_summary
from app.services.missions.mission_truth_gate import (
    TruthGateCode,
    gate_decision,
    visible_step_count,
)

# Estados de ciclo de vida que el SEP aún no representa — se reflejan desde
# ``Mission.status`` solo para etiquetas, no para ejecutabilidad semántica.
_WORKFLOW_FROM_PERSISTED: FrozenSet[str] = frozenset({
    MissionStatus.RECORDING.value,
    MissionStatus.PAUSED.value,
    MissionStatus.INTERPRETING.value,
    MissionStatus.FAILED.value,
})


@dataclass(frozen=True)
class MissionUIExecutionState:
    """Snapshot coherente para widgets (lista, detalle, banners)."""

    intent_status: str
    is_executable: bool
    visible_blockers: Tuple[str, ...]
    ready_provenance: str
    persisted_status_value: str
    gate_blockers: Tuple[str, ...]

    @property
    def needs_human_attention(self) -> bool:
        return bool(self.visible_blockers)

    @property
    def ui_bucket(self) -> str:
        """READY | EXECUTABLE | NEEDS_REVIEW | UNSAFE | DRAFT."""
        if self.is_executable:
            return (
                "READY" if self.intent_status == "READY" else "EXECUTABLE"
            )
        if self.visible_blockers:
            return "NEEDS_REVIEW"
        gb = set(self.gate_blockers)
        if (
            TruthGateCode.PLAN_INVALID_STRICT in gb
            or TruthGateCode.LAYER_MISMATCH in gb
            or TruthGateCode.NO_SEMANTIC_PLAN in gb
        ):
            return "UNSAFE"
        # Plan semántico existente pero bloqueado solo por flags técnicos /
        # borradores sin código humano visible.
        if self.intent_status in ("", "COMPILED_DRAFT"):
            return "DRAFT"
        return "UNSAFE"


def compute_mission_ui_execution_state(mission: Any) -> MissionUIExecutionState:
    """Calcula estado UI — ``gate_decision`` refresca el SEP; el resumen lee
    ``visible_blockers`` sin volver a recomputar el intent salvo que el
    caller haya mutado la misión entre medias."""
    dec = gate_decision(mission)
    summary = build_mission_review_summary(mission, recompute=False)
    sep = getattr(mission, "semantic_execution_plan", None) or {}
    intent = str(sep.get("intent_status") or "")
    prov = str(sep.get("ready_provenance") or summary.provenance_key or "")
    persisted = str(
        getattr(getattr(mission, "status", None), "value", "") or "",
    )
    return MissionUIExecutionState(
        intent_status=intent,
        is_executable=bool(dec.is_executable),
        visible_blockers=tuple(summary.visible_blockers),
        ready_provenance=prov,
        persisted_status_value=persisted,
        gate_blockers=tuple(dec.blockers),
    )


def mission_ui_should_offer_execute_and_test(mission: Any) -> bool:
    """Habilitar «Ejecutar» / «Probar misión» en vista normal."""
    pv = str(
        getattr(getattr(mission, "status", None), "value", "") or "",
    )
    if pv in (
        MissionStatus.RECORDING.value,
        MissionStatus.INTERPRETING.value,
    ):
        return False
    return compute_mission_ui_execution_state(mission).is_executable


def mission_ui_detail_status_line(mission: Any, *, expert_mode: bool = False) -> str:
    """Texto de estado para panel detalle / tooltips (con emoji).

    ``expert_mode=True`` añade pistas técnicas del truth gate (códigos GATE_*).
    """
    n = visible_step_count(mission)
    pv = str(
        getattr(getattr(mission, "status", None), "value", "") or "",
    )
    if pv == MissionStatus.RECORDING.value:
        return f"🔴 Grabando · {n} pasos"
    if pv == MissionStatus.PAUSED.value:
        return f"⏸ Pausada · {n} pasos"
    if pv == MissionStatus.INTERPRETING.value:
        return f"🧠 Interpretando · {n} pasos"
    if pv == MissionStatus.FAILED.value:
        return f"❌ Error · {n} pasos"

    st = compute_mission_ui_execution_state(mission)
    if st.is_executable:
        line = f"✅ Lista para ejecutar · {n} pasos"
        if expert_mode:
            rep = getattr(mission, "one_shot_reliability_report", None) or {}
            if isinstance(rep, dict) and rep.get("one_shot_reliability_score") is not None:
                line += f" · OSS={float(rep['one_shot_reliability_score']):.2f}"
            uool = getattr(mission, "uool_orchestration_report", None) or {}
            if isinstance(uool, dict) and uool.get("unified_confidence") is not None:
                line += f" · UOC={float(uool['unified_confidence']):.2f}"
            ocrp = getattr(mission, "ocrp_repeatability_report", None) or {}
            if isinstance(ocrp, dict) and ocrp.get("repeatability_score") is not None:
                line += f" · REP={float(ocrp['repeatability_score']):.2f}"
        return line

    bucket = st.ui_bucket
    labels = {
        "NEEDS_REVIEW": "⚠ Necesita aclaración",
        "UNSAFE": "❌ No es seguro continuar",
        "DRAFT": "🔧 Adaptar este paso",
    }
    prefix = labels.get(bucket, "🔧 Adaptar este paso")
    base = f"{prefix} · {n} pasos"
    if expert_mode and st.gate_blockers:
        gb = ", ".join(st.gate_blockers[:6])
        return f"{base} · gate=[{gb}]"
    return base


def mission_ui_list_icon(mission: Any) -> str:
    """Icono compacto en lista lateral."""
    pv = str(
        getattr(getattr(mission, "status", None), "value", "") or "",
    )
    if pv == MissionStatus.RECORDING.value:
        return "🔴"
    if pv == MissionStatus.INTERPRETING.value:
        return "🧠"
    st = compute_mission_ui_execution_state(mission)
    if st.is_executable:
        return "✅"
    if st.needs_human_attention:
        return "📝"
    return "⚠️"


def mission_ui_list_tooltip_suffix(mission: Any) -> str:
    """Fragmento de tooltip coherente con el gate (no ``status`` crudo)."""
    pv = str(
        getattr(getattr(mission, "status", None), "value", "") or "",
    )
    if pv in _WORKFLOW_FROM_PERSISTED:
        return f"workflow={pv}"
    st = compute_mission_ui_execution_state(mission)
    if st.is_executable:
        return "ejecutable · SEP"
    return f"{st.ui_bucket.lower()} · provenance={st.ready_provenance}"


def mission_ui_shows_in_attention_filter(mission: Any) -> bool:
    """«Requieren atención»: bloqueos humanos visibles (truth gate), no
    ``pending_review`` persistido aislado."""
    pv = str(
        getattr(getattr(mission, "status", None), "value", "") or "",
    )
    if pv in (
        MissionStatus.RECORDING.value,
        MissionStatus.INTERPRETING.value,
    ):
        return False
    st = compute_mission_ui_execution_state(mission)
    return st.needs_human_attention


def format_product_execution_hint(
    mission: Any,
    *,
    expert_mode: bool = False,
) -> str:
    """Copy corto para vista normal (sin mecánica) vs experto (auditoría one-shot)."""
    rep = getattr(mission, "one_shot_reliability_report", None) or {}
    if expert_mode and isinstance(rep, dict) and rep.get("preflight_outcome"):
        return (
            f"{rep.get('preflight_outcome')} · "
            f"plan={rep.get('plan_source')} · "
            f"runtime={rep.get('runtime_mode')} · "
            f"OSS={rep.get('one_shot_reliability_score')}"
        )
    st = compute_mission_ui_execution_state(mission)
    if st.is_executable:
        return "Lista para ejecutar"
    if st.ui_bucket == "NEEDS_REVIEW":
        return "Necesita aclaración"
    if st.ui_bucket == "UNSAFE":
        return "No es seguro continuar"
    return "Adaptar este paso"


__all__ = [
    "MissionUIExecutionState",
    "compute_mission_ui_execution_state",
    "mission_ui_detail_status_line",
    "mission_ui_list_icon",
    "mission_ui_list_tooltip_suffix",
    "mission_ui_should_offer_execute_and_test",
    "mission_ui_shows_in_attention_filter",
    "format_product_execution_hint",
]
