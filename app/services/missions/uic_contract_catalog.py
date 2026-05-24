"""
Catálogo **canónico** de filas UIC ↔ runtime (fuente para tests).

La tabla humana vive en ``docs/uic_contract_matrix.md``; cualquier
desviación respecto a ``uic_capability_registry`` debe resolverse
actualizando el registry o estas filas.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Iterable, List, Tuple

from app.services.missions.uic_capability_registry import (
    get_capability,
)


@dataclass(frozen=True)
class UICContractRow:
    capability_id: str
    sep_type: str
    sipe_promoted_type: str
    mission_step_kind: str
    runner_strategy_primary: str
    outcome_validator: str
    legacy_aliases: Tuple[str, ...] = ()


UIC_CONTRACT_ROWS: Tuple[UICContractRow, ...] = (
    UICContractRow(
        capability_id="open_app",
        sep_type="open_app",
        sipe_promoted_type="open_app",
        mission_step_kind="open_app",
        runner_strategy_primary="open_app:windows_search",
        outcome_validator="_passthrough_validator",
        legacy_aliases=(),
    ),
    UICContractRow(
        capability_id="open_url",
        sep_type="open_url",
        sipe_promoted_type="open_site",
        mission_step_kind="open_url",
        runner_strategy_primary="open_url:hotkey_ctrl_l",
        outcome_validator="_passthrough_validator",
        legacy_aliases=("open_site",),
    ),
    UICContractRow(
        capability_id="open_site",
        sep_type="open_site",
        sipe_promoted_type="open_site",
        mission_step_kind="open_site",
        runner_strategy_primary="open_site:smart_route",
        outcome_validator="_passthrough_validator",
        legacy_aliases=("open_url",),
    ),
    UICContractRow(
        capability_id="search_content",
        sep_type="search_content",
        sipe_promoted_type="search_site",
        mission_step_kind="search_content",
        runner_strategy_primary="search_content:smart_route",
        outcome_validator="_need_provider_or_site",
        legacy_aliases=("search_youtube",),
    ),
    UICContractRow(
        capability_id="search_site",
        sep_type="search_site",
        sipe_promoted_type="search_site",
        mission_step_kind="search_site",
        runner_strategy_primary="search_site:smart_route",
        outcome_validator="_need_query",
        legacy_aliases=("search_youtube", "search_content"),
    ),
    UICContractRow(
        capability_id="search_youtube",
        sep_type="search_youtube",
        sipe_promoted_type="search_site",
        mission_step_kind="search_youtube",
        runner_strategy_primary="search_youtube:dom_input",
        outcome_validator="_need_query",
        legacy_aliases=("search_content",),
    ),
    UICContractRow(
        capability_id="scroll_results",
        sep_type="scroll_results",
        sipe_promoted_type="scroll_results",
        mission_step_kind="scroll_results",
        runner_strategy_primary="scroll_results:wheel",
        outcome_validator="_passthrough_validator",
        legacy_aliases=(),
    ),
    UICContractRow(
        capability_id="select_profile",
        sep_type="select_profile",
        sipe_promoted_type="select_profile",
        mission_step_kind="select_profile",
        runner_strategy_primary="select_profile:uia_text_match",
        outcome_validator="_need_profile",
        legacy_aliases=(),
    ),
)


def validate_uic_catalog_against_registry() -> List[str]:
    """Devuelve lista de errores humanos (vacía si todo alinea)."""
    errs: List[str] = []
    seen_caps: FrozenSet[str] = frozenset()
    for row in UIC_CONTRACT_ROWS:
        cap = get_capability(row.capability_id)
        if cap is None:
            errs.append(f"fila '{row.capability_id}': capability ausente en registry")
            continue
        primaries = tuple(cap.preferred_strategies)
        if not primaries:
            errs.append(f"{row.capability_id}: registry sin estrategias preferidas")
            continue
        reg_primary = primaries[0]
        if reg_primary != row.runner_strategy_primary:
            errs.append(
                f"{row.capability_id}: primaria registry={reg_primary!r} "
                f"≠ catálogo={row.runner_strategy_primary!r}",
            )
        ov_name = getattr(cap.outcome_validator, "__name__", repr(cap.outcome_validator))
        if ov_name != row.outcome_validator:
            errs.append(
                f"{row.capability_id}: outcome_validator registry={ov_name} "
                f"≠ catálogo={row.outcome_validator}",
            )
        seen_caps = seen_caps | {row.capability_id}

    # Capacidades obligatorias según contrato cerrado Nevlan (subset crítico).
    required = {"search_content", "search_site", "search_youtube"}
    missing_req = required - seen_caps
    for m in sorted(missing_req):
        errs.append(f"catálogo sin fila obligatoria: {m}")

    return errs


def iter_catalog_capability_ids(rows: Iterable[UICContractRow]) -> FrozenSet[str]:
    return frozenset(r.capability_id for r in rows)


__all__ = [
    "UICContractRow",
    "UIC_CONTRACT_ROWS",
    "validate_uic_catalog_against_registry",
    "iter_catalog_capability_ids",
]
