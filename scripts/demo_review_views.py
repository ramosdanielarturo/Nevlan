"""Demo de las dos vistas de Mission Review (normal vs experto).

Usa la misión REAL ``mission_1778221896``. Aplica las
confirmaciones humanas y muestra cómo se vería en cada modo.
"""
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
    except Exception:
        pass

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission,
)
from app.services.missions.mission_review_confirm import (
    confirm_profile, confirm_query,
)
from app.services.missions.mission_review_summary import (
    build_mission_review_summary,
    render_expert_view,
    render_normal_view,
)

RAW_PATH = Path(
    "tests/fixtures/real_missions/mission_1778221896_raw.json"
)


def main() -> None:
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    data = copy.deepcopy(raw)
    data.pop("semantic_execution_plan", None)
    data["compiled_execution_graph"] = []
    data["interpreted_steps"] = []
    data["action_groups"] = []
    data["status"] = "draft"
    data["review_confirmations"] = []
    m = Mission(**data)
    apply_collapse_to_mission(m)
    confirm_profile(m, profile_name="Daniel Arturo Ramos")
    confirm_query(m, query="Devu\u00e9lveme el amor de Luis Miguel")

    summary = build_mission_review_summary(m)

    print("=" * 70)
    print("VISTA NORMAL  (lo que el usuario común ve)")
    print("=" * 70)
    print(render_normal_view(summary))
    print()
    print("=" * 70)
    print("VISTA EXPERTO  (con panel 'Confirmado por usuario')")
    print("=" * 70)
    print(render_expert_view(summary))


if __name__ == "__main__":
    main()
