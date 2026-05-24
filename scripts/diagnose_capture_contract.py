"""
diagnose_capture_contract.py
=============================

Imprime el contract de captura para el último click sobre un proceso de
navegador (chrome / msedge / firefox / etc.) en la misión más reciente
de ``var/missions/``. Útil para confirmar — sin tests — que un nuevo
recording llega con evidencia visual real (pre_snapshot, ocr_lines,
parent_chain, post_state) o que el recorder declaró ``capture_incomplete``
honestamente.

Uso::

    python scripts/diagnose_capture_contract.py
    python scripts/diagnose_capture_contract.py path/al/mission.json

NO modifica nada. NO requiere pytest.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


_BROWSER_PROCS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "browser.exe",
}


def _latest_mission(missions_dir: Path) -> Optional[Path]:
    files = sorted(
        [p for p in missions_dir.glob("*.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _last_click_on_browser(raw_trace: List[Dict[str, Any]]) -> Optional[int]:
    last_idx: Optional[int] = None
    for i, ev in enumerate(raw_trace):
        et = (ev.get("event_type") or "").lower()
        if "click" not in et:
            continue
        wc = ev.get("window_context") or {}
        proc = (wc.get("process_name") or "").lower()
        if proc in _BROWSER_PROCS or "chrome" in proc or "edge" in proc:
            last_idx = i
    return last_idx


def main(argv: List[str]) -> int:
    here = Path(__file__).resolve().parent.parent
    if len(argv) >= 2:
        mission_path = Path(argv[1])
    else:
        missions = here / "var" / "missions"
        mission_path = _latest_mission(missions) if missions.exists() else None
    if mission_path is None or not mission_path.exists():
        print("no se encontró ninguna misión en var/missions/")
        return 2
    print(f"=== Misión: {mission_path} ===")
    data = json.loads(mission_path.read_text(encoding="utf-8"))
    raw = data.get("raw_trace") or []
    print(f"name={data.get('name')!r}  raw_trace_len={len(raw)}")

    idx = _last_click_on_browser(raw)
    if idx is None:
        print("no hay clicks sobre un navegador en esta misión.")
        return 0
    ev = raw[idx]
    meta = ev.get("metadata") or {}
    wc = ev.get("window_context") or {}
    print()
    print(f"--- último click sobre navegador (idx={idx}) ---")
    print(f"raw_event_id  = {ev.get('id')}")
    print(f"sequence_id   = {ev.get('sequence_id')}")
    print(f"window.title  = {wc.get('title')!r}")
    print(f"window.proc   = {wc.get('process_name')!r}")
    ma = ev.get("mouse_action") or {}
    print(f"click_xy      = ({ma.get('x')}, {ma.get('y')})")
    print()
    print("--- pre-action snapshot ---")
    pre_path = meta.get("pre_action_snapshot_full")
    print(f"pre_action_snapshot_full   = {pre_path!r}")
    print(f"pre_action_age_ms          = {meta.get('pre_action_age_ms')!r}")
    if pre_path:
        print(f"  exists={Path(pre_path).exists()}")
    print()
    print("--- post-action state ---")
    pas = meta.get("post_action_state") or {}
    print(f"post_action_state present  = {bool(pas)}")
    if pas:
        print(f"  detected_change = {pas.get('detected_change')}")
        print(f"  latency_ms      = {pas.get('latency_ms')}")
        print(f"  window_title    = {pas.get('window_title')!r}")
        print(f"  url             = {pas.get('url')!r}")
    print()
    print("--- OCR (vision_data) ---")
    vd = meta.get("vision_data") or {}
    lines = vd.get("ocr_lines") or []
    print(f"ocr_lines (n) = {len(lines)}")
    for i, ln in enumerate(lines[:8]):
        print(f"  [{i}] {ln!r}")
    print(f"ocr_text_around = {(vd.get('ocr_text_around') or '')[:160]!r}")
    print(f"ocr_source      = {vd.get('ocr_source')!r}")
    print()
    print("--- UIA + parent_chain ---")
    uia = meta.get("uia") or {}
    print(f"uia.name          = {uia.get('name')!r}")
    print(f"uia.control_type  = {uia.get('control_type')!r}")
    print(f"uia.automation_id = {uia.get('automation_id')!r}")
    pc = uia.get("parent_chain") or {}
    ancestors = pc.get("ancestors") or []
    siblings = pc.get("siblings_near_click") or []
    print(f"parent_chain.ancestors        (n) = {len(ancestors)}")
    for i, a in enumerate(ancestors[:4]):
        print(f"  [{i}] name={a.get('name')!r} ct={a.get('control_type')!r}")
    print(f"parent_chain.siblings_near_click (n) = {len(siblings)}")
    for i, s in enumerate(siblings[:6]):
        print(f"  [{i}] name={s.get('name')!r} ct={s.get('control_type')!r}")
    print()
    print("--- VisualTargetIdentity (live) ---")
    sys.path.insert(0, str(here))
    try:
        from app.contracts.mission import Mission  # type: ignore
        from app.services.missions.screen_element_reader import (
            read_visual_target,
        )
        m = Mission.model_validate(data)
        ev_obj = m.raw_trace[idx]
        vti = read_visual_target(ev_obj)
        print(f"visual_type       = {vti.visual_type!r}")
        print(f"primary_text      = {vti.primary_text!r}")
        print(f"secondary_text    = {vti.secondary_text!r}")
        print(f"click_inside_bbox = {vti.click_inside_bbox}")
        print(f"evidence_level    = {vti.evidence_level!r}")
        print(f"sources_used      = {vti.sources_used!r}")
    except Exception as e:
        print(f"(no se pudo invocar el reader: {e})")
    print()
    print("--- capture_contract ---")
    contract = meta.get("capture_contract") or {}
    if not contract:
        print("(sin capture_contract — el recorder antiguo no lo escribió)")
    else:
        for k in ("has_pre_snapshot", "has_post_state", "has_uia",
                  "has_parent_chain", "has_ocr_or_text",
                  "capture_complete", "sufficient_for_execution",
                  "stable_automation_id", "collateral_execution"):
            print(f"  {k:18s}= {contract.get(k)}")
        print(f"  missing           = {contract.get('missing')}")
        print(f"  execution_missing = {contract.get('execution_missing')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
