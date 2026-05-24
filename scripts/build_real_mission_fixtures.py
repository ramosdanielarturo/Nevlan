"""
Genera fixtures de misiones REALES capturadas por el usuario para los
tests de regresión.

Cada misión real se materializa en DOS fixtures (raw + confirmed):

  * ``tests/fixtures/real_missions/<name>_raw.json``
        Estado tal como lo dejó la grabación humana real.
        ``intent_status == NEEDS_REVIEW``.
        ``review_confirmations == []``.
        ``raw_trace`` intacto.

  * ``tests/fixtures/real_missions/<name>_confirmed.json``
        Estado tras Mission Review (confirm_profile + confirm_query).
        ``intent_status == READY``.
        ``review_confirmations`` con N entradas estructuradas.
        ``raw_trace`` IDÉNTICO al fixture raw (verificado).

Adicionalmente, restauramos los archivos ``var/missions/<id>.json`` a
su estado honesto (``intent_status == NEEDS_REVIEW``) — el archivo
"live" jamás debe mostrar READY si la grabación venía incompleta.

Idempotente: ejecutar varias veces produce el mismo resultado.

Misiones reales registradas
---------------------------

  * ``mission_1778221896`` — Chrome → perfil → YouTube → buscar
    canción → scroll. Caso original del PRD 2026-05-08.

  * ``mission_1778244908`` — misma intención, otra captura del
    usuario. Caso PRD 2026-05-08c (cierre LEGACY_RUNTIME_CONTAMINATION).
    El bug original: el JSON live persistido afirmaba
    ``LEGACY_RUNTIME_CONTAMINATION`` aunque la lógica actual ya no
    debía emitirlo. Esta fixture documenta el estado limpio.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace",
        )
    except Exception:
        pass

from app.contracts.mission import Mission, mission_to_dict
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.mission_review_confirm import (
    confirm_profile, confirm_query,
)


FIXTURES_DIR = Path("tests/fixtures/real_missions")


@dataclass
class RealMissionSpec:
    """Metadata para regenerar las fixtures de una misión real."""

    name: str                          # ej. "mission_1778221896"
    live_path: Path                    # var/missions/<uuid>.json
    profile_name: str = ""             # vacío ⇒ no se aplica confirm_profile
    query: str = ""                    # vacío ⇒ no se aplica confirm_query
    # Si la misión cae naturalmente en READY tras
    # ``apply_collapse_to_mission`` (raw_evidence completo, sin
    # campos faltantes), el _raw.json YA es la fuente de verdad y
    # no hay flujo "confirm" que documentar.
    expects_ready_from_raw: bool = False

    @property
    def raw_fixture(self) -> Path:
        return FIXTURES_DIR / f"{self.name}_raw.json"

    @property
    def confirmed_fixture(self) -> Path:
        return FIXTURES_DIR / f"{self.name}_confirmed.json"


REAL_MISSIONS: List[RealMissionSpec] = [
    RealMissionSpec(
        name="mission_1778221896",
        live_path=Path("var/missions/82bec5fd-cefb-45db-a82d-4bf074bf4180.json"),
        profile_name="Daniel Arturo Ramos",
        query="Devu\u00e9lveme el amor de Luis Miguel",
    ),
    RealMissionSpec(
        name="mission_1778244908",
        live_path=Path("var/missions/2d1fec16-aafd-41bd-980b-3e40e91d4ee4.json"),
        profile_name="Daniel Arturo Ramos",
        query="Devu\u00e9lveme el amor de Luis Miguel",
    ),
    # mission_1778297133 (PRD 2026-05-08c §exec-lifecycle): la
    # grabación cae directo en READY con ``ready_provenance="raw_evidence"``.
    # El usuario escribió "devuelveme el amor luis miguel" tal cual y
    # confirma que esa query es la correcta — **no es bug**. Por eso
    # NO aplicamos confirm_query/confirm_profile aquí: solo persistimos
    # el _raw.json y restauramos el live a su estado honesto.
    RealMissionSpec(
        name="mission_1778297133",
        live_path=Path("var/missions/c77ce517-994e-4095-852d-a98dafd3b6c1.json"),
        expects_ready_from_raw=True,
    ),
    # mission_1778303937 (PRD 2026-05-08d §A — perfil vacío):
    # el raw_trace #4 trae UIA contaminado por contenido YouTube
    # (``rendering-content`` / ``ytd-in-feed-ad-layout-renderer``).
    # Tras los filtros nuevos del ``profile_resolver`` y del ICE,
    # el plan queda en NEEDS_REVIEW con ``profile_name=""`` y
    # ``needs_user_label=True`` para activar la UI "Confirmar perfil".
    # El _confirmed se genera con la confirmación del usuario.
    RealMissionSpec(
        name="mission_1778303937",
        live_path=Path("var/missions/87752f25-a2bd-4b64-acd6-b766beb19c84.json"),
        profile_name="Daniel Arturo Ramos",
        query="Devu\u00e9lveme el amor de Luis Miguel",
    ),
]


def _load_live(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"falta el JSON live {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _build_raw_state(live: dict) -> dict:
    """Reconstruye el estado natural NEEDS_REVIEW desde el raw_trace.

    Intencionalmente:
      * limpia ``semantic_execution_plan`` (lo regeneramos)
      * limpia ``review_confirmations`` (no había confirmaciones)
      * mantiene ``raw_trace`` intacto
      * resetea status a "draft" antes del replay
    """
    data = copy.deepcopy(live)
    data.pop("semantic_execution_plan", None)
    data["review_confirmations"] = []
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["tags"] = [
        t for t in (data.get("tags") or [])
        if not str(t).startswith("review_confirmed:")
    ]
    m = Mission(**data)
    apply_collapse_to_mission(m)
    out = mission_to_dict(m)
    return out


def _build_confirmed_state(raw_state: dict, spec: RealMissionSpec) -> dict:
    """Aplica el flujo Mission Review sobre la misión raw."""
    m = Mission(**copy.deepcopy(raw_state))
    confirm_profile(m, profile_name=spec.profile_name)
    confirm_query(m, query=spec.query)
    out = mission_to_dict(m)
    sep = out.get("semantic_execution_plan") or {}
    if sep.get("intent_status") != "READY":
        raise SystemExit(
            f"Tras confirm, esperaba READY, fue "
            f"{sep.get('intent_status')!r}"
        )
    if sep.get("not_ready_reasons"):
        raise SystemExit(
            f"Tras confirm, not_ready_reasons debe estar vacío, "
            f"fue {sep.get('not_ready_reasons')!r}"
        )
    return out


def _assert_raw_trace_unchanged(raw_state: dict, confirmed: dict) -> None:
    """raw_trace en el fixture confirmed debe ser BYTE-IDENTICO al raw."""
    a = json.dumps(raw_state.get("raw_trace"), ensure_ascii=False, sort_keys=True)
    b = json.dumps(confirmed.get("raw_trace"), ensure_ascii=False, sort_keys=True)
    if a != b:
        raise SystemExit(
            "FAIL: raw_trace cambió tras confirmar. "
            "Mission Review NO debe tocar la evidencia."
        )


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  -> {path} ({len(json.dumps(payload))} bytes)")


def _process_mission(spec: RealMissionSpec) -> Tuple[dict, dict]:
    print(f"\n========================")
    print(f"  {spec.name}")
    print(f"========================")
    live = _load_live(spec.live_path)

    raw_state = _build_raw_state(live)
    raw_status = raw_state["semantic_execution_plan"]["intent_status"]

    if spec.expects_ready_from_raw:
        if raw_status != "READY":
            raise SystemExit(
                f"{spec.name}: esperaba READY desde raw_evidence pero "
                f"el ICE produjo {raw_status!r}. Revisar antes de fixar."
            )
        print(f"\n[fixture raw — READY natively]")
        _write(spec.raw_fixture, raw_state)
        print(f"  intent_status        : "
              f"{raw_state['semantic_execution_plan']['intent_status']!r}")
        print(f"  not_ready_reasons    : "
              f"{raw_state['semantic_execution_plan']['not_ready_reasons']}")
        print(f"  ready_provenance     : "
              f"{raw_state['semantic_execution_plan'].get('ready_provenance')!r}")
        # No hay confirmed: la fixture raw es la fuente de verdad.
        confirmed = raw_state
        print(f"\n[live] Restaurando {spec.live_path} a su estado honesto ...")
        spec.live_path.write_text(
            json.dumps(raw_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  -> {spec.live_path} "
              f"(status={raw_state['status']!r}, "
              f"intent_status={raw_status!r})")
        return raw_state, confirmed

    if raw_status != "NEEDS_REVIEW":
        raise SystemExit(
            f"El raw_trace ya NO produce NEEDS_REVIEW "
            f"({raw_status!r}); algo cambió en el ICE. "
            "Revisar antes de fixar fixture."
        )

    confirmed = _build_confirmed_state(raw_state, spec)
    _assert_raw_trace_unchanged(raw_state, confirmed)

    print(f"\n[fixture raw]")
    _write(spec.raw_fixture, raw_state)
    print(f"  intent_status        : "
          f"{raw_state['semantic_execution_plan']['intent_status']!r}")
    print(f"  not_ready_reasons    : "
          f"{raw_state['semantic_execution_plan']['not_ready_reasons']}")
    print(f"  review_confirmations : "
          f"{len(raw_state.get('review_confirmations') or [])}")

    print(f"\n[fixture confirmed]")
    _write(spec.confirmed_fixture, confirmed)
    print(f"  intent_status        : "
          f"{confirmed['semantic_execution_plan']['intent_status']!r}")
    print(f"  review_confirmations : "
          f"{len(confirmed.get('review_confirmations') or [])} entries")
    for c in confirmed.get("review_confirmations") or []:
        print(f"    - field={c['field']!r:14s} value={c['value']!r:42s} "
              f"source={c['source']!r}")

    print(f"\n[live] Restaurando {spec.live_path} a NEEDS_REVIEW honesto ...")
    spec.live_path.write_text(
        json.dumps(raw_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  -> {spec.live_path} "
          f"(status={raw_state['status']!r}, "
          f"intent_status="
          f"{raw_state['semantic_execution_plan']['intent_status']!r})")

    return raw_state, confirmed


def main() -> None:
    print("Generando fixtures de misiones reales ...")
    for spec in REAL_MISSIONS:
        if not spec.live_path.exists():
            print(f"\n[skip] {spec.name}: falta {spec.live_path}")
            continue
        _process_mission(spec)
    print("\nOK.")


if __name__ == "__main__":
    main()
