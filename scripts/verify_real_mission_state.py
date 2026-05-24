"""Imprime el estado actual de mission_1778221896 tras --write."""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace",
)
d = json.loads(
    Path("var/missions/82bec5fd-cefb-45db-a82d-4bf074bf4180.json")
    .read_text(encoding="utf-8"),
)
sep = d["semantic_execution_plan"]
print(f"mission.name              : {d['name']!r}")
print(f"mission.status            : {d['status']!r}")
print(f"sep.intent_status         : {sep['intent_status']!r}")
print(f"sep.not_ready_reasons     : {sep['not_ready_reasons']}")
print(f"sep.coords_used           : {sep['coords_used']}")
print(f"sep.legacy_graph_ignored  : {sep['legacy_graph_ignored']}")
print(
    f"mission.legacy_graph_purpose: "
    f"{d.get('legacy_compiled_graph_purpose')!r}"
)
sp = sep["steps"][1]
print()
print("step[1] select_profile:")
print(f"  human_label             : {sp['human_label']!r}")
print(f"  params.profile_name     : {sp['params']['profile_name']!r}")
print(f"  needs_user_label        : {sp['needs_user_label']}")
sp = sep["steps"][4]
print()
print("step[4] search_youtube:")
print(f"  human_label             : {sp['human_label']!r}")
print(f"  params.query            : {sp['params']['query']!r}")
print(f"  params.fidelity         : {sp['params']['query_fidelity_status']!r}")
print(f"  needs_user_label        : {sp['needs_user_label']}")
