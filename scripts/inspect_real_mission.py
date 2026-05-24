"""Quick dump of mission_1778221896 raw_trace events for analysis."""
import json
import sys
from pathlib import Path

PATH = Path("var/missions/82bec5fd-cefb-45db-a82d-4bf074bf4180.json")
d = json.loads(PATH.read_text(encoding="utf-8"))
print(f"mission name={d.get('name')!r} status={d.get('status')!r}")
print(f"raw_trace_count={len(d.get('raw_trace') or [])}")
print(f"compiled_count={len(d.get('compiled_execution_graph') or [])}")
print(f"legacy_compiled_graph_purpose={d.get('legacy_compiled_graph_purpose')!r}")
print()
print("=" * 78)
print("RAW TRACE")
print("=" * 78)
for i, ev in enumerate(d["raw_trace"]):
    et = ev.get("event_type")
    wc = ev.get("window_context") or {}
    md = ev.get("metadata") or {}
    title = (wc.get("title") or "")[:60]
    proc = wc.get("process_name") or ""
    print(f"#{i:02d} {et:32s} proc={proc!r:22s} title={title!r}")
    uia = md.get("uia") or md.get("uia_data") or {}
    if uia:
        n = (uia.get("name") or "")[:60]
        print(f"      uia.name={n!r} ctype={uia.get('control_type')!r}")
    web = md.get("web") or md.get("web_data") or {}
    if web:
        url = (web.get("url") or web.get("href") or "")[:60]
        print(f"      web.url={url!r} role={web.get('role')!r} ph={web.get('placeholder')!r}")
    for k in ("final_text", "text", "value", "typed_text"):
        if md.get(k):
            print(f"      meta.{k}={md.get(k)!r}")
    if md.get("field_commit"):
        print(f"      field_commit={md.get('field_commit')}")
    for k in ("human_observed_label", "user_action_label",
              "profile_picker_item_text"):
        if md.get(k):
            print(f"      meta.{k}={md.get(k)!r}")
    if ev.get("keyboard_action"):
        ka = ev["keyboard_action"]
        keys = (ka.get("keys") or [])[:30]
        print(f"      keys={keys} mods={ka.get('modifiers')}")
    print()

print("=" * 78)
print("COMPILED GRAPH (only types/labels)")
print("=" * 78)
for i, c in enumerate(d.get("compiled_execution_graph") or []):
    pl = c.get("action_payload") or {}
    print(f"#{i:02d} {c.get('action_strategy'):28s} pl_keys={list(pl.keys())[:10]}")
    for k in ("text", "query", "profile", "profile_name",
             "user_confirmed_label", "needs_user_label", "label_prompt"):
        if k in pl:
            print(f"      pl.{k}={pl.get(k)!r}")
    sig = c.get("target_signature") or {}
    if sig:
        for k in ("uia_name", "control_type", "asset_path",
                 "visual_asset_path"):
            if sig.get(k):
                print(f"      sig.{k}={sig.get(k)!r}")
    print()
