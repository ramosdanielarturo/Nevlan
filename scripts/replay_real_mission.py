"""Re-run the ICE on mission_1778221896 raw_trace using the current
code path. Shows what the system produces TODAY without touching the
persisted plan.
"""
import json
from pathlib import Path

from app.contracts.mission import Mission
from app.services.missions.intent_collapse_engine import (
    apply_collapse_to_mission, collapse_intent,
)
from app.services.missions.semantic_execution_plan import (
    attach_intent_status_to_plan, build_semantic_execution_plan,
)

PATH = Path("var/missions/82bec5fd-cefb-45db-a82d-4bf074bf4180.json")
data = json.loads(PATH.read_text(encoding="utf-8"))

# Recreate mission from raw_trace ONLY (clear plan + compiled to
# force regeneration with current code path).
data.pop("semantic_execution_plan", None)
data["compiled_execution_graph"] = []
data["interpreted_steps"] = []
data["action_groups"] = []
data["status"] = "draft"

m = Mission(**data)
print(f"=== Mission name={m.name!r}, raw_trace_count={len(m.raw_trace)} ===")
apply_collapse_to_mission(m)

sep = m.semantic_execution_plan
print(f"=== intent_status={sep.get('intent_status')!r} ===")
print(f"=== not_ready_reasons={sep.get('not_ready_reasons')} ===")
print(f"=== coords_used={sep.get('coords_used')} ===")
print(f"=== legacy_graph_ignored={sep.get('legacy_graph_ignored')} ===")
print(f"=== legacy_compiled_graph_purpose={m.legacy_compiled_graph_purpose!r} ===")
print()
for i, sp in enumerate(sep["steps"]):
    print(f"#{i:02d} type={sp['type']:18s} preferred={sp['preferred_strategy']!r}")
    print(f"     params={sp.get('params')}")
    if sp.get("needs_user_label"):
        print(f"     NEEDS_LABEL prompt={sp.get('label_prompt')!r}")
    print()
