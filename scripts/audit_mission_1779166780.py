"""Audit mission_1779166780 raw_trace for PCOF gaps."""
import json
from pathlib import Path

p = Path("var/missions/bd11499d-ca08-4a36-b20d-235bafa6f457.json")
m = json.loads(p.read_text(encoding="utf-8"))
trace = m.get("raw_trace") or []
print("total events", len(trace))
for i, ev in enumerate(trace):
    et = ev.get("event_type")
    meta = ev.get("metadata") or {}
    wc = ev.get("window_context") or {}
    proc = wc.get("process_name") or ""
    title = (wc.get("title") or "")[:60]
    fr = meta.get("operational_freeze_snapshot") or {}
    best = fr.get("best_entity_candidate") or {}
    name = (best.get("display_name") or "")[:50]
    coll = fr.get("best_collection_candidate") or {}
    ents = coll.get("entities") or []
    ma = ev.get("mouse_action") or {}
    xy = (ma.get("x"), ma.get("y"))
    ts = ev.get("timestamp", "")[:19]
    print(
        f"{i:3d} {ts} {et:18s} {proc:12s} {title:35s} "
        f"xy={xy} ent={name!r} coll={len(ents)} pre={fr.get('pre_transition_confirmed')}"
    )
