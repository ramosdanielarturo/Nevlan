"""
Humanización del panel de regrabación — sin microhistoria técnica por defecto.
"""
from __future__ import annotations

from typing import List, Tuple, Optional

from app.contracts.mission import EventType, RawEvent
from app.services.missions.compiler import MissionCompiler, _canonical_key, _is_special_key


def _proc_title(ev: RawEvent) -> str:
    wc = getattr(ev, "window_context", None)
    if not wc:
        return ""
    pn = (getattr(wc, "process_name", None) or "").strip()
    if pn:
        return pn.replace(".exe", "").replace(".EXE", "").title()[:32]
    t = (getattr(wc, "title", None) or "").strip()
    return (t[:52] + "…") if len(t) > 52 else t


def _hotkey_human(ev: RawEvent) -> str:
    ka = ev.keyboard_action
    if not ka:
        return "⌨️ Tecla"
    keys = ka.keys or [""]
    mods_l = [str(m).lower() for m in (ka.modifiers or [])]
    key = _canonical_key(keys[0])
    lk = ".".join(str(k).lower() for k in keys)
    mh = "".join(mods_l)
    if ("ctrl" in mh or lk.startswith("ctrl")) and key == "s":
        return "💾 Guardar (Ctrl+S)"
    if ("ctrl" in mh or lk.startswith("ctrl")) and key == "l":
        return "🔎 Barra de dirección / búsqueda (Ctrl+L)"
    if ("alt" in mh) and key == "tab":
        return "🪟 Cambiar de ventana (Alt+Tab)"
    if key == "tab":
        if "shift" in mh:
            return "⇤ Shift+Tab"
        return "⇥ Tab"
    combo = "+".join(mods_l + [keys[0].upper()]) if mods_l else (keys[0].upper())
    return f"⌨️ {combo}"


def _tail_text_keystrokes_count(events: List[RawEvent]) -> int:
    n = 0
    for ev in reversed(events):
        if ev.event_type != EventType.KEYBOARD_KEY_PRESS or not ev.keyboard_action:
            break
        ka = ev.keyboard_action
        k0 = (ka.keys or [""])[0]
        mods = [str(m).lower() for m in (ka.modifiers or [])]
        if [m for m in mods if m != "shift"]:
            break
        kc = _canonical_key(k0)
        if kc == "backspace" and len(mods) == 0:
            n += 1
            continue
        if kc == "space":
            n += 1
            continue
        if len(k0) == 1 and not _is_special_key(k0):
            n += 1
            continue
        break
    return n


def build_rerecord_display(
    raw_events: List[RawEvent],
    *,
    expert: bool = False,
) -> Tuple[str, str]:
    clean = MissionCompiler._resolve_text_sessions(list(raw_events))
    lines: List[str] = []
    raw_lines: List[str] = []

    i = 0
    while i < len(clean):
        ev = clean[i]

        if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
            md = getattr(ev, "metadata", None) or {}
            ft = str(md.get("final_text") or "")
            short = ft if len(ft) <= 64 else ft[:62] + "…"
            if ft.strip():
                lines.append(f'📝 Texto capturado: “{short}”')
            raw_lines.append(f"type_text:{ft!r}")
            i += 1
            continue

        if ev.event_type == EventType.KEYBOARD_KEY_PRESS and ev.keyboard_action:
            k = _canonical_key((ev.keyboard_action.keys or [""])[0])
            if k == "tab":
                n_tab = 1
                j = i + 1
                while j < len(clean):
                    e2 = clean[j]
                    if (
                        e2.event_type == EventType.KEYBOARD_KEY_PRESS
                        and e2.keyboard_action
                        and _canonical_key((e2.keyboard_action.keys or [""])[0]) == "tab"
                    ):
                        n_tab += 1
                        j += 1
                    else:
                        break
                if n_tab >= 2:
                    lines.append(f"⇥ Presionar TAB × {n_tab}")
                else:
                    lines.append(_hotkey_human(ev))
                raw_lines.extend(["tab"] * n_tab)
                i = j
                continue

            line = _hotkey_human(ev)
            lines.append(line)
            ka = ev.keyboard_action
            raw_lines.append(
                "|".join(ka.keys or []) + "+" + "|".join(ka.modifiers or [])
            )
            i += 1
            continue

        if ev.event_type == EventType.MOUSE_CLICK:
            ua = {}
            try:
                ua = (ev.metadata or {}).get("uia") if isinstance(ev.metadata, dict) else {}
            except Exception:
                ua = {}
            ua = ua or {}
            nm = (ua.get("name") or "").strip()
            proc = _proc_title(ev)
            tgt = nm[:44] + ("…" if len(nm) > 44 else "") if nm else ""
            if nm and proc:
                lines.append(f"🖱️ Clic en “{tgt}” · {proc}")
            elif nm:
                lines.append(f"🖱️ Clic en “{tgt}”")
            elif proc:
                lines.append(f"🖱️ Clic en {proc}")
            else:
                lines.append("🖱️ Clic detectado")
            ma = ev.mouse_action
            if ma:
                raw_lines.append(f"mouse {getattr(ma,'x',0)} {getattr(ma,'y',0)}")
            i += 1
            continue

        et = getattr(ev.event_type, "value", str(ev.event_type))
        raw_lines.append(et)
        i += 1

    tail_txt = _tail_text_keystrokes_count(raw_events)
    if tail_txt >= 1:
        pending = "⌛ Detectando texto en campo…"
        insert_at = max(0, len(lines) - 3)
        if pending not in lines:
            lines.insert(insert_at, pending)

    vis = lines[-14:]
    human = (
        "**Regrabación en curso**\n\n" + ("\n".join(vis) if vis else "")
    ).strip()
    if not vis and tail_txt:
        human += "\n\n⌛ Detectando texto en campo…"

    raw_blk = ""
    if expert:
        raw_blk = "\n".join(raw_lines[-30:])

    return human, raw_blk


def banner_line() -> str:
    return "Ejecuta la nueva acción en la app objetivo — interpretamos en humano."
