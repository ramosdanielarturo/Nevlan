"""Demo end-to-end usando la grabacion REAL mission_1778221896."""
from __future__ import annotations

import copy
import io
import json
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace",
        )
        sys.stderr = io.TextIOWrapper(
            sys.stderr.buffer, encoding="utf-8", errors="replace",
        )
    except Exception:
        pass

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.mission_review_confirm import (
    confirm_profile, confirm_query,
)

PATH = Path("var/missions/82bec5fd-cefb-45db-a82d-4bf074bf4180.json")


def _print_state(label: str, mission: Mission) -> None:
    sep = mission.semantic_execution_plan or {}
    print()
    print("=" * 78)
    print(f"  {label}")
    print("=" * 78)
    print(f"  intent_status      : {sep.get('intent_status')!r}")
    print(f"  not_ready_reasons  : {sep.get('not_ready_reasons')}")
    print(f"  coords_used        : {sep.get('coords_used')}")
    print(f"  legacy_graph_ignored: {sep.get('legacy_graph_ignored')}")
    print(
        f"  legacy_graph_purpose: "
        f"{mission.legacy_compiled_graph_purpose!r}"
    )
    print()
    for i, sp in enumerate(sep.get("steps") or []):
        flag = ""
        if sp.get("needs_user_label"):
            flag = " [NEEDS CONFIRM] " + (sp.get("label_prompt") or "")
        print(f"  {i + 1}. {sp.get('human_label'):46s}{flag}")
    print()


def main() -> None:
    raw = json.loads(PATH.read_text(encoding="utf-8"))
    print(f"\nMision real cargada: {raw['name']!r}")
    print(f"raw_trace events: {len(raw.get('raw_trace') or [])}")

    data = copy.deepcopy(raw)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    m = Mission(**data)

    apply_collapse_to_mission(m)
    _print_state("BEFORE - sin confirmacion humana", m)

    print(">>> Mission Review confirma profile = 'Daniel Arturo Ramos'")
    confirm_profile(m, profile_name="Daniel Arturo Ramos")

    print(
        ">>> Mission Review confirma query   = "
        "'Devuelveme el amor de Luis Miguel' "
        "(con tilde original aceptada)"
    )
    confirm_query(m, query="Devu\u00e9lveme el amor de Luis Miguel")

    _print_state("AFTER - confirmacion aplicada", m)

    sep = m.semantic_execution_plan
    if sep["intent_status"] == "READY":
        print("[OK] La mision REAL paso a READY.")
        print("     'Grabar una vez, ejecutar siempre.'")
    else:
        print("[FAIL] La mision REAL no llego a READY.")
        print(f"       Razones: {sep['not_ready_reasons']}")

    if "--write" in sys.argv:
        # Sobrescribe la grabacion real con el plan canonico ya
        # confirmado (perfil + query). Util para validar que el
        # SmartExecutor recibe READY en una proxima ejecucion.
        out_data = json.loads(PATH.read_text(encoding="utf-8"))
        out_data["semantic_execution_plan"] = sep
        out_data["status"] = (
            "executable" if sep["intent_status"] == "READY"
            else out_data.get("status")
        )
        PATH.write_text(
            json.dumps(out_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[--write] persistido en {PATH}")


if __name__ == "__main__":
    main()
