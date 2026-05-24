"""
Nevlan Compiler Engine
----------------------
Transforma RawEvents (clicks, teclas, scroll, drag) en un CompiledExecutionGraph.

Mejoras clave de esta versión:
- Hotkeys reales reconocidas por modifiers (`keyboard_action.modifiers`) → ej. ctrl+c.
- Agrupación de teclas especiales repetidas (TAB TAB TAB → {key:tab, repeat:3}).
- Agrupación de caracteres normales → TYPE_TEXT.
- Soporte para scroll (ActionStrategy.SCROLL) y drag&drop (ActionStrategy.DRAG_DROP).
- Descripciones humanas claras:
    * ctrl+s → "💾 Guardar (Ctrl+S)"
    * ctrl+c → "📋 Copiar (Ctrl+C)"
    * ctrl+v → "📋 Pegar (Ctrl+V)"
    * alt+tab → "🪟 Cambiar de ventana"
    * tab repetido → "Tabular N veces"
- Normalización de nombres pynput (ctrl_l → ctrl, escape → esc, cmd → win).
- Hereda window_context al CompiledStep para que el player reactive la ventana correcta.
"""
from __future__ import annotations

from typing import List, Dict, Any, Optional, Tuple
import uuid
from datetime import datetime, timezone

from app.contracts.mission import (
    Mission, RawEvent, CompiledStep, TargetContext, TargetResolutionStrategy,
    ActionStrategy, ValidationStrategy, FallbackStrategy,
    EventType, MissionStatus, InterpretedStep, ActionGroup,
)
from app.services.missions.step_quality import compute_step_quality

# Sentinel para ordenar eventos sin timestamp al final de forma determinista.
_NO_TS_SENTINEL = datetime.max.replace(tzinfo=timezone.utc)


# ──────────────────────────────────────────────────────────────────────
# Normalización de nombres de tecla
# ──────────────────────────────────────────────────────────────────────

_KEY_ALIASES = {
    "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift_l": "shift", "shift_r": "shift",
    "cmd": "win", "cmd_l": "win", "cmd_r": "win",
    "escape": "esc", "enter": "enter", "return": "enter",
    "space": "space", "backspace": "backspace", "delete": "delete",
    "key.backspace": "backspace", "key.delete": "delete",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "tab": "tab", "caps_lock": "capslock", "print_screen": "printscreen",
    "insert": "insert",
}

_SPECIAL_KEYS = {
    "tab", "enter", "esc", "escape", "space", "backspace", "delete",
    "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
    "capslock", "caps_lock", "printscreen", "insert",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
}


def _canonical_key(k: str) -> str:
    """Devuelve el nombre canónico de una tecla."""
    if not k:
        return ""
    k2 = k.strip().lower()
    if k2.startswith("key."):
        k2 = k2[4:]
    return _KEY_ALIASES.get(k2, k2)


def _is_special_key(k: str) -> bool:
    c = _canonical_key(k)
    return c in _SPECIAL_KEYS or len(c) > 1


def _describe_hotkey(modifiers: List[str], key: str, repeat: int = 1) -> str:
    """Descripción humana bonita para una hotkey."""
    mods = [m.lower() for m in modifiers or []]
    k = _canonical_key(key)
    combo = "+".join([m.capitalize() for m in mods] + [k.upper() if k else ""]).strip("+")
    semantic = {
        ("ctrl", "s"): "💾 Guardar (Ctrl+S)",
        ("ctrl", "c"): "📋 Copiar (Ctrl+C)",
        ("ctrl", "v"): "📋 Pegar (Ctrl+V)",
        ("ctrl", "x"): "✂ Cortar (Ctrl+X)",
        ("ctrl", "z"): "↶ Deshacer (Ctrl+Z)",
        ("ctrl", "y"): "↷ Rehacer (Ctrl+Y)",
        ("ctrl", "a"): "Seleccionar todo (Ctrl+A)",
        ("ctrl", "f"): "🔎 Buscar (Ctrl+F)",
        ("ctrl", "l"): "Enfocar barra de direcciones (Ctrl+L)",
        ("ctrl", "t"): "Nueva pestaña (Ctrl+T)",
        ("ctrl", "w"): "Cerrar pestaña (Ctrl+W)",
        ("alt", "tab"): "🪟 Cambiar de ventana (Alt+Tab)",
        ("alt", "f4"): "✖ Cerrar ventana (Alt+F4)",
        ("shift", "tab"): "⇤ Navegar atrás (Shift+Tab)",
    }
    name = semantic.get((tuple(mods)[0] if len(mods) == 1 else "", k), None) or \
           semantic.get((mods[0] if mods else "", k), None)
    if not name:
        if not mods:
            if k == "enter":
                name = "✅ Confirmar (Enter)"
            elif k == "esc":
                name = "✕ Cancelar (Esc)"
            elif k == "tab":
                name = "Navegar al siguiente campo (Tab)"
            else:
                name = f"Presionar {combo or k.upper()}"
        else:
            name = f"Presionar {combo}"
    if repeat > 1:
        name += f" × {repeat}"
    return name


# ──────────────────────────────────────────────────────────────────────
# MissionCompiler
# ──────────────────────────────────────────────────────────────────────

def _mission_step_quality(score: float) -> str:
    if score >= 80:
        return "green"
    if score >= 50:
        return "yellow"
    return "red"


# Default ValidationStrategy por ActionStrategy. PRD §6: ningún step puede
# salir con `NONE` — el ApprovalGate lo refuse. Las elecciones reflejan
# qué señal post-acción tiene mejor relación coste/beneficio en el player.
_DEFAULT_VALIDATION: Dict[ActionStrategy, ValidationStrategy] = {
    ActionStrategy.CLICK: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.DOUBLE_CLICK: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.RIGHT_CLICK: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.TYPE_TEXT: ValidationStrategy.REQUIRE_FIELD_VALUE,
    ActionStrategy.SET_FIELD_VALUE: ValidationStrategy.REQUIRE_FIELD_VALUE,
    ActionStrategy.SEND_HOTKEY: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.DRAG_DROP: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.SCROLL: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.SCROLL_UNTIL_VISIBLE: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.SELECT_OPTION: ValidationStrategy.REQUIRE_TEXT_MATCH,
    ActionStrategy.OPEN_MENU_ITEM: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.CONFIRM_DIALOG: ValidationStrategy.REQUIRE_DISAPPEAR,
    ActionStrategy.NAVIGATE_OR_SEARCH: ValidationStrategy.REQUIRE_TEXT_MATCH,
    ActionStrategy.SUBMIT_SEARCH: ValidationStrategy.REQUIRE_TEXT_MATCH,
    ActionStrategy.LAUNCH_APP: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.SELECT_PROFILE: ValidationStrategy.REQUIRE_TEXT_MATCH,
    ActionStrategy.OPEN_NEW_TAB: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.OPEN_BOOKMARK: ValidationStrategy.REQUIRE_TEXT_MATCH,
    ActionStrategy.WAIT_FOR_STATE: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.EXTRACT_DATA: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    ActionStrategy.AGENT_PROMPT: ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
}


def _default_validation_for(action: ActionStrategy) -> ValidationStrategy:
    return _DEFAULT_VALIDATION.get(action, ValidationStrategy.REQUIRE_ELEMENT_EXISTS)


def _looks_like_ctrl_s_save(cs: CompiledStep) -> bool:
    if cs.action_strategy != ActionStrategy.SEND_HOTKEY:
        return False
    pl = cs.action_payload or {}
    keys = [str(x).lower() for x in (pl.get("keys") or [])]
    if pl.get("combo") and keys and keys[-1] == "s":
        return any(x.startswith("ctrl") for x in keys[:-1])
    return str(pl.get("hotkey") or "").lower() == "s"


def _group_pattern_ctrl_l_search(
    sid_to: Dict[str, CompiledStep], step_ids: List[str],
) -> bool:
    """Ctrl+L (barra de dirección) dentro del grupo → “Buscar o navegar”."""
    seq = [sid_to[s] for s in step_ids if s in sid_to]
    for cs in seq:
        if cs.action_strategy != ActionStrategy.SEND_HOTKEY:
            continue
        pl = cs.action_payload or {}
        keys = [str(x).lower() for x in (pl.get("keys") or [])]
        if pl.get("combo") and keys and keys[-1] == "l":
            return any(x.startswith("ctrl") for x in keys[:-1])
    return False


def _group_pattern_form_click_type_tab(
    sid_to: Dict[str, CompiledStep], step_ids: List[str],
) -> bool:
    """Campo + texto + Tab/Enter — patrón de llenado de formulario."""
    seq = [sid_to[s] for s in step_ids if s in sid_to]
    if len(seq) < 2:
        return False
    has_anchor_click = False
    for cs in seq:
        if cs.action_strategy == ActionStrategy.CLICK:
            ct = (
                ((cs.target_context.uia_data or {}).get("control_type") or "")
                .lower()
            )
            if ct and ("edit" in ct or ct == "combo"):
                has_anchor_click = True
                break
    textish = sum(
        1
        for cs in seq
        if cs.action_strategy
        in (ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE)
    )
    tab_enter = False
    for cs in seq:
        if cs.action_strategy != ActionStrategy.SEND_HOTKEY:
            continue
        pl = cs.action_payload or {}
        kk = ",".join(str(x).lower() for x in (pl.get("keys") or []))
        if any(x in kk for x in ("tab", "enter", "return")):
            tab_enter = True
            break
    return textish >= 1 and tab_enter and (has_anchor_click or textish >= 1)


def _group_click_cluster(sid_to: Dict[str, CompiledStep], step_ids: List[str]) -> bool:
    """Varios clicks consecutivos sobre controles ⇒ “Seleccionar opciones”."""
    seq = [sid_to[s] for s in step_ids if s in sid_to]
    n_click = sum(1 for x in seq if x.action_strategy == ActionStrategy.CLICK)
    return n_click >= 3


def _count_tab_hotkeys_in_group(sid_to: Dict[str, CompiledStep], step_ids: List[str]) -> int:
    n = 0
    for sid in step_ids:
        cs = sid_to.get(sid)
        if not cs or cs.action_strategy != ActionStrategy.SEND_HOTKEY:
            continue
        pl = cs.action_payload or {}
        kk = ",".join(str(x).lower() for x in (pl.get("keys") or []))
        if "tab" in kk:
            n += int(pl.get("repeat") or 1)
    return n


def _semantic_group_human_summary(
    sid_to: Dict[str, CompiledStep],
    step_ids: List[str],
    strats: List[ActionStrategy],
    *,
    semantic_tag: Optional[str],
    ctrl_l_grp: bool,
    fill_pat: bool,
    clicks_cluster: bool,
    n_nav: int,
    n_set: int,
    many_tabs: int,
) -> str:
    """Resumen en lenguaje natural para revisión rápida (no sustituye título)."""
    n = len(step_ids)
    if not n:
        return ""
    seq = [sid_to[s] for s in step_ids if s in sid_to]

    if semantic_tag == "save":
        return "Confirmar guardado del archivo abierto."

    has_scroll_click = semantic_tag == "scroll_click" or (
        ActionStrategy.SCROLL in strats and ActionStrategy.CLICK in strats
    )
    if has_scroll_click or semantic_tag == "scroll_find":
        return "Buscar y seleccionar un elemento visible en pantalla (scroll + clic)."
    if ctrl_l_grp:
        return "Buscar o navegar a Google/usar la barra de dirección y seguir navegación."
    if fill_pat and n >= 4 and n_set >= 2:
        return f"Rellenar formulario con varios campos (~{max(n_set, 2)}) y confirmar navegación en el formulario."

    if many_tabs >= 3 or (
        semantic_tag == "form" and ActionStrategy.SEND_HOTKEY in strats and many_tabs >= 2
    ):
        return "Navegar el formulario u opciones usando principalmente Tab/Enter."

    if semantic_tag == "navigate_search" or n_nav >= 1 or ctrl_l_grp:
        return "Exploración o uso de campo de URL/búsqueda del navegador."

    if n_set >= 4:
        return f"Introducir datos en ~{n_set} campos del mismo flujo."

    if n_set >= 2:
        return f"Llenado de formulario corto (~{n_set} campos) en pantalla."

    if clicks_cluster or (semantic_tag == "multi_click" and ActionStrategy.CLICK in strats):
        return "Seleccionar una o más opciones haciendo varios clics seguidos."

    if semantic_tag == "menu":
        return "Abrir menú y elegir un elemento dentro de él."

    if ActionStrategy.SCROLL_UNTIL_VISIBLE in strats:
        return "Desplazar hasta localizar contenido antes de interactuar."

    return f"{n} pasos relacionados dentro del mismo contexto de aplicación."


def recompile_mission(mission: Mission) -> Mission:
    """Recompila una misión EXISTENTE usando su raw_trace.

    Úsalo para migrar misiones ya guardadas al nuevo pipeline (hotkeys con
    modifiers, patrones, scroll, drag, normalización). Si no hay raw_trace,
    devuelve la misión tal cual (no rompe automatizaciones sin trazas).
    """
    if not mission.raw_trace:
        return mission
    # Limpiamos el grafo y los pasos interpretados; raw_trace se conserva.
    mission.compiled_execution_graph = []
    mission.interpreted_steps = []
    return MissionCompiler.compile_mission(mission)


class MissionCompiler:

    @staticmethod
    def _build_action_groups_heuristic(
        compiled: List[CompiledStep], interp: List[InterpretedStep]
    ) -> List[ActionGroup]:
        """Agrupa pasos compilados por ventana/objetivo humano sin borrar grafo técnico."""
        if not compiled:
            return []
        out: List[ActionGroup] = []
        bucket_indices: List[int] = []
        max_per_group = 8

        def flush_bucket() -> None:
            if not bucket_indices:
                return
            scs = []
            for bi in bucket_indices:
                cf = compiled[bi].target_context.confidence or {}
                scs.append(float(cf.get("capture_score") or 50.0))
            conf_avg = round(sum(scs) / max(1, len(scs)), 1)
            sid = [compiled[bi].id for bi in bucket_indices]
            fi = bucket_indices[0]
            raw_title = (
                interp[fi].description
                if fi < len(interp) and (interp[fi].description or "").strip()
                else str(compiled[fi].action_strategy.value)
            )
            t_clean = raw_title.strip()
            title = t_clean[:54] + ("…" if len(t_clean) > 54 else "")
            out.append(
                ActionGroup(
                    group_title=title,
                    group_summary=f"{len(sid)} paso(s) en mismo contexto",
                    step_ids=sid,
                    confidence_score=conf_avg,
                    status="ok",
                )
            )

        prev_proc_marker: Optional[str] = None
        for idx, cs in enumerate(compiled):
            proc = (cs.target_context.process_name or "").strip().lower()

            boundary = False
            if bucket_indices:
                bp = (
                    compiled[bucket_indices[0]].target_context.process_name
                    or ""
                ).strip().lower()
                if proc and bp and proc != bp:
                    boundary = True

            overflow = len(bucket_indices) >= max_per_group

            if bucket_indices and (boundary or overflow):
                flush_bucket()
                bucket_indices = []

            bucket_indices.append(idx)
            prev_proc_marker = proc or prev_proc_marker

        flush_bucket()
        return [g for g in out if g.step_ids]

    @staticmethod
    def _post_pass_keyboard_only_fill(
        compiled: List[CompiledStep], interp: List[InterpretedStep]
    ) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
        """Solo KEYBOARD_TYPE_TEXT con field_label fuerte → SET_FIELD_VALUE."""
        for i, cs in enumerate(compiled):
            if cs.action_strategy != ActionStrategy.TYPE_TEXT:
                continue
            pl = dict(cs.action_payload or {})
            if not pl.get("field_session"):
                continue
            label = str(pl.get("field_label") or "").strip()
            if len(label) < 2:
                continue
            prev = compiled[i - 1] if i > 0 else None
            if prev and prev.action_strategy in (
                ActionStrategy.CLICK, ActionStrategy.SET_FIELD_VALUE,
            ):
                continue
            cs.action_strategy = ActionStrategy.SET_FIELD_VALUE
            txt = str(pl.get("text") or "")
            short = txt if len(txt) <= 40 else txt[:40] + "…"
            cs.action_payload = {
                "text": txt,
                "label": label,
                "control_type_hint": "",
                "web_role_hint": "",
                "web_tag_hint": "",
                "fusion_confidence": "keyboard_only_strong_label",
            }
            cs.validation_strategy = ValidationStrategy.REQUIRE_FIELD_VALUE
            if i < len(interp):
                interp[i].description = f"📝 Rellenar campo '{label}' con '{short}'"
        return compiled, interp

    @staticmethod
    def _collapse_accidental_search_launcher_drag(
        compiled: List[CompiledStep],
        interp: List[InterpretedStep],
    ) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
        """Drag en SearchUI + type_text → foco en cuadro de búsqueda, no drag_drop."""
        if not compiled:
            return compiled, interp
        out_c: List[CompiledStep] = []
        out_i: List[InterpretedStep] = []
        i = 0
        while i < len(compiled):
            c = compiled[i]
            proc = (c.target_context.process_name or "").strip().lower()
            is_drag = c.action_strategy == ActionStrategy.DRAG_DROP
            is_search_ui = proc in ("searchui.exe", "searchhost.exe")
            if is_drag and is_search_ui and i + 1 < len(compiled):
                nxt = compiled[i + 1]
                pl = dict(nxt.action_payload or {})
                if (
                    nxt.action_strategy == ActionStrategy.TYPE_TEXT
                    and pl.get("field_session")
                ):
                    c.action_strategy = ActionStrategy.CLICK
                    c.action_payload = {
                        "search_launcher_focus": True,
                        "launcher_action": "focus_search_launcher_input",
                        "note": "collapsed_from_accidental_drag",
                    }
                    if i < len(interp):
                        interp[i].description = "Enfocar cuadro de búsqueda"
                    out_c.append(c)
                    out_i.append(interp[i] if i < len(interp) else InterpretedStep())
                    i += 1
                    continue
            out_c.append(c)
            out_i.append(interp[i] if i < len(interp) else InterpretedStep())
            i += 1
        return out_c, out_i

    @staticmethod
    def _build_semantic_action_groups(
        compiled: List[CompiledStep], interp: List[InterpretedStep],
    ) -> List[ActionGroup]:
        """Heurística + títulos más humanos encima del particionado por proceso."""
        base = MissionCompiler._build_action_groups_heuristic(compiled, interp)
        sid_to = {c.id: c for c in compiled}
        out: List[ActionGroup] = []

        interp_by_step_id = {
            compiled[i].id: interp[i]
            for i in range(min(len(compiled), len(interp)))
        }

        for g in base:
            strats: List[ActionStrategy] = []
            reds = 0
            scores: List[float] = []
            for sid in g.step_ids:
                cs = sid_to.get(sid)
                if not cs:
                    continue
                strats.append(cs.action_strategy)
                cf = cs.target_context.confidence or {}
                scores.append(float(cf.get("capture_score") or 50.0))
                i_s = interp_by_step_id.get(sid)
                if compute_step_quality(cs, i_s).level == "red":
                    reds += 1

            title = g.group_title
            tag: Optional[str] = None
            n_set = sum(1 for x in strats if x == ActionStrategy.SET_FIELD_VALUE)
            n_nav = sum(1 for x in strats if x == ActionStrategy.NAVIGATE_OR_SEARCH)
            ctrl_l_grp = _group_pattern_ctrl_l_search(sid_to, g.step_ids)
            fill_pat = _group_pattern_form_click_type_tab(sid_to, g.step_ids)
            clicks_cluster = _group_click_cluster(sid_to, g.step_ids)
            many_tabs = _count_tab_hotkeys_in_group(sid_to, g.step_ids)

            if ctrl_l_grp:
                title = "Buscar o navegar a Google"
                tag = "navigate_search"
            elif fill_pat:
                title = "Rellenar campo o formulario"
                tag = "form_fill_pattern"
            elif clicks_cluster:
                title = "Seleccionar opciones"
                tag = "multi_click"
            elif n_nav >= 1:
                title = "Buscar o navegar"
                tag = "navigate"
            elif n_set >= 2:
                title = "Llenar formulario"
                tag = "form"
            elif ActionStrategy.SCROLL_UNTIL_VISIBLE in strats:
                title = "Buscar elemento en pantalla"
                tag = "scroll_find"
            elif ActionStrategy.SCROLL in strats and ActionStrategy.CLICK in strats:
                title = "Desplazar y elegir"
                tag = "scroll_click"
            elif any(
                _looks_like_ctrl_s_save(sid_to[s])
                for s in g.step_ids
                if s in sid_to
            ):
                title = "Guardar archivo"
                tag = "save"
            elif any(
                sid_to.get(s) and sid_to[s].action_strategy == ActionStrategy.OPEN_MENU_ITEM
                for s in g.step_ids
            ):
                title = "Seleccionar en menú"
                tag = "menu"

            grp_ql = "red" if reds else ("yellow" if scores and min(scores) < 60 else "green")
            warn: List[str] = []
            if reds:
                warn.append(f"{reds} paso(s) marcados como débiles en este bloque")

            summary_txt = _semantic_group_human_summary(
                sid_to,
                g.step_ids,
                strats,
                semantic_tag=tag,
                ctrl_l_grp=ctrl_l_grp,
                fill_pat=fill_pat,
                clicks_cluster=clicks_cluster,
                n_nav=n_nav,
                n_set=n_set,
                many_tabs=many_tabs,
            )

            out.append(
                g.model_copy(
                    update={
                        "group_title": title,
                        "semantic_tag": tag,
                        "quality_level": grp_ql,
                        "warnings": warn,
                        "expanded_default": len(g.step_ids) <= 4,
                        "group_summary": summary_txt,
                    }
                )
            )
        return out

    @staticmethod
    def compile_mission(mission: Mission) -> Mission:
        mission.status = MissionStatus.INTERPRETING

        # ── Pre-sort por (timestamp, sequence_id) ──────────────────
        # Los threads de pynput (mouse vs keyboard) pueden entregar
        # eventos en orden distinto al real. Ordenamos por timestamp
        # con ``sequence_id`` como tie-breaker — el grabador asigna un
        # contador monotónico que sirve para desempatar cuando varios
        # eventos comparten el mismo timestamp (frecuente en captura
        # rápida: e.g. click + window_activate al abrir Chrome).
        raw = list(mission.raw_trace or [])
        try:
            raw.sort(
                key=lambda ev: (
                    ev.timestamp or _NO_TS_SENTINEL,
                    int(getattr(ev, "sequence_id", 0) or 0),
                )
            )
        except Exception:
            pass  # Si no hay timestamp, mantenemos el orden original

        # ── Pass -1: Raw Event Sanitizer (V2) ──────────────────────
        # Antes de cualquier pase de fusión, limpiamos eventos
        # claramente basura: clicks duplicados, contenedores genéricos
        # sin valor semántico, scrolls repetidos, fragmentos de texto
        # dentro de la misma sesión. Reduce el ruido que el resto del
        # pipeline tiene que parchear caso por caso.
        try:
            from app.services.missions.raw_event_sanitizer import (
                sanitize_raw_trace,
            )
            from app.core.logger import log as _log_v2
            cleaned, sreport = sanitize_raw_trace(raw)
            _log_v2.info(f"[compiler] sanitizer: {sreport.as_dict()}")
            raw = cleaned
        except Exception as e:
            try:
                from app.core.logger import log as _log_v2
                _log_v2.debug(f"[compiler] sanitizer falló (no bloqueante): {e}")
            except Exception:
                pass

        mission.raw_trace = raw

        # Pass 0: resolver sesiones de edición de campo (Field Editing Session).
        # Convierte secuencias tipo "h" "e" "l" "o" BACKSPACE "l" "o" en UN solo
        # evento sintético con el VALOR FINAL tipeado ("hello"), ignorando ruido
        # (backspaces, shift como modificador textual).
        trace = MissionCompiler._resolve_text_sessions(mission.raw_trace)
        aggregated = MissionCompiler._aggregate_events(trace)

        compiled_steps: List[CompiledStep] = []
        interpreted_steps: List[InterpretedStep] = []

        for group in aggregated:
            if not group:
                continue
            c_step, i_step = MissionCompiler._compile_group(group, mission)
            if c_step is None:
                continue
            compiled_steps.append(c_step)
            interpreted_steps.append(i_step)

        # Pattern-pass: Windows Search → LAUNCH_APP debe ir PRIMERO,
        # antes de que ``_post_pass_patterns`` fusione el click sobre la
        # caja de búsqueda con el texto en SET_FIELD_VALUE — eso enmascara
        # el patrón "abrir app" y el bug Chrome resurge.
        compiled_steps, interpreted_steps = MissionCompiler._post_pass_windows_search_launch(
            compiled_steps, interpreted_steps
        )

        # Pattern-pass: detectar "llenar campo" (click en Edit + type_text)
        compiled_steps, interpreted_steps = MissionCompiler._post_pass_patterns(
            compiled_steps, interpreted_steps
        )

        # Pattern-pass V3: detectar patrones semánticos extra
        # (NAVIGATE_OR_SEARCH, SUBMIT_SEARCH, OPEN_MENU_ITEM, SCROLL_UNTIL_VISIBLE).
        compiled_steps, interpreted_steps = MissionCompiler._post_pass_patterns_v3(
            compiled_steps, interpreted_steps
        )

        compiled_steps, interpreted_steps = MissionCompiler._post_pass_keyboard_only_fill(
            compiled_steps, interpreted_steps
        )

        # ── Semantic Fusion Pass (PRD 2026-05-03) ──────────────────────
        # Implementa los hooks A/B/C: invalid target filter, chrome
        # workflow (Win Search rescue + select_profile + open_new_tab +
        # open_bookmark) y field session merger v2 (puente sobre Enter).
        # Vive en módulo aparte para mantener este compilador legible.
        from app.services.missions.semantic_fusion import apply_semantic_fusion
        compiled_steps, interpreted_steps = apply_semantic_fusion(
            compiled_steps, interpreted_steps,
        )

        # Compactar TYPE_TEXT/SET_FIELD_VALUE consecutivos del mismo
        # campo cuando uno es prefijo del siguiente. Esto absorbe
        # fragmentos como "D" + "Devuelveme..." que aparecían cuando
        # el listener emitía una sesión de texto múltiples veces
        # (típicamente por toggles legacy de CapsLock).
        compiled_steps, interpreted_steps = MissionCompiler._post_pass_collapse_partial_text(
            compiled_steps, interpreted_steps
        )

        # ── Invalid Target Rescue (V2) ─────────────────────────────
        # Tras la fusión semántica clásica, intentamos rescatar steps
        # cuyo target sigue siendo inválido (guide-service, main,
        # GroupControl vacío). Convertimos clicks débiles en acciones
        # semánticas fuertes (select_profile / open_new_tab /
        # focus_search / scroll_results) cuando el contexto lo permite.
        try:
            from app.services.missions.invalid_target_resolver import (
                rescue_steps as _rescue_steps,
            )
            from app.core.logger import log as _log_v2
            rescued_compiled, rescued_n = _rescue_steps(compiled_steps)
            if rescued_n:
                _log_v2.info(
                    f"[compiler] invalid_target_resolver rescued {rescued_n} "
                    f"steps (de {len(compiled_steps)})"
                )
            # Mantener interpretados alineados con el grafo final.
            if len(rescued_compiled) == len(compiled_steps):
                compiled_steps = rescued_compiled
        except Exception as e:
            try:
                from app.core.logger import log as _log_v2
                _log_v2.debug(f"[compiler] invalid_target_resolver falló: {e}")
            except Exception:
                pass

        # Mejorar labels técnicos (guide-service, divs anónimos, etc.)
        # cuando hay alternativas humanas (placeholder, aria-label).
        for c, i in zip(compiled_steps, interpreted_steps):
            MissionCompiler._humanize_field_label(c, i)

        # Capture score + semantic intent por paso (FASE 4 PRD).
        for c, i in zip(compiled_steps, interpreted_steps):
            MissionCompiler._enrich_semantic_and_score(c, i)

        # Normalización final: asegura payloads y descripciones válidas por paso
        for c, i in zip(compiled_steps, interpreted_steps):
            MissionCompiler._normalize_step(c, i)

        compiled_steps, interpreted_steps = (
            MissionCompiler._collapse_accidental_search_launcher_drag(
                compiled_steps, interpreted_steps,
            )
        )

        mission.action_groups = MissionCompiler._build_semantic_action_groups(
            compiled_steps, interpreted_steps
        )

        # Relink
        for i in range(len(compiled_steps) - 1):
            compiled_steps[i].next_step_id_on_success = compiled_steps[i + 1].id

        mission.compiled_execution_graph = compiled_steps
        mission.interpreted_steps = interpreted_steps
        # Estado puente: la misión está compilada técnicamente; los assets
        # visuales ya pasaron por ``finalize_mission_assets`` al final de
        # este método. Falta el ``approval_gate`` antes de ``EXECUTABLE``.
        mission.status = MissionStatus.COMPILED

        # ── Semantic handoff: ejecuta el Intent Collapse Engine para
        # producir y persistir el ``semantic_execution_plan``. (PRD
        # 2026-05-07 §1.)
        #
        # MODO NO-DESTRUCTIVO (PRD §10): el grafo legacy queda intacto
        # (lo siguen consumiendo player legacy + tests del compiler
        # clásico para casos donde el ICE no aplica). El plan
        # semántico se persiste y queda marcado como fuente única de
        # ejecución. ``legacy_compiled_graph_purpose`` se marca
        # ``legacy_debug_only`` cuando el plan es válido.
        #
        # El ICE es defensivo: si no logra colapsar (raw_trace muy
        # raro), no toca el grafo legacy y se queda como única fuente.
        try:
            from app.services.missions.intent_collapse_engine import (
                apply_collapse_to_mission,
            )
            apply_collapse_to_mission(mission, rewrite_compiled_graph=False)
            try:
                from app.services.missions.semantic_execution_plan import (
                    SemanticExecutionPlan,
                    attach_intent_status_to_plan,
                )
                from app.services.runtime.universal_collection_entity_engine import (
                    apply_uces_primary_compilation,
                )

                blob = mission.semantic_execution_plan
                if isinstance(blob, dict) and blob.get("steps"):
                    uces_plan = SemanticExecutionPlan.from_dict(blob)
                    if apply_uces_primary_compilation(uces_plan, mission):
                        attach_intent_status_to_plan(uces_plan, mission=mission)
                        mission.semantic_execution_plan = uces_plan.to_dict()
            except Exception as uces_exc:
                try:
                    from app.core.logger import log as _log_uces
                    _log_uces.debug(
                        f"[compiler] uces primary compilation: {uces_exc}"
                    )
                except Exception:
                    pass
        except Exception as e:
            try:
                from app.core.logger import log as _log_handoff
                _log_handoff.debug(
                    f"[compiler] semantic_handoff falló (no bloqueante): {e}"
                )
            except Exception:
                pass

        # PRD 2026-05-10d (Mission Truth Gate §3 + §5): si el plan
        # semántico no es READY, la misión NO puede quedarse marcada
        # como ``COMPILED`` (que la UI antigua tratará como ejecutable).
        # El gate la baja a ``NEEDS_REVIEW`` para que ni el executor
        # ni el Centro de Automatizaciones la disparen.
        try:
            from app.services.missions.mission_truth_gate import (
                reconcile_mission_status,
            )
            reconcile_mission_status(mission)
        except Exception as e:
            try:
                from app.core.logger import log as _log_gate
                _log_gate.debug(
                    f"[compiler] mission_truth_gate falló (no bloqueante): {e}"
                )
            except Exception:
                pass

        # Promover PNGs de ``assets/temp/`` a ``assets/{mission.id}/`` y
        # reescribir referencias antes de persistir/ejecutar. Sin esto el
        # JSON puede quedar con rutas ``/temp/`` huérfanas si Mission Review
        # no abrió el finalizer explícito.
        try:
            from app.services.missions.asset_finalizer import (
                finalize_mission_assets,
            )
            finalize_mission_assets(mission)
        except Exception as e:
            try:
                from app.core.logger import log as _log_finalize
                _log_finalize.debug(
                    f"[compiler] finalize_mission_assets falló (no bloqueante): {e}"
                )
            except Exception:
                pass

        return mission

    @staticmethod
    def rebuild_action_groups(mission: Mission) -> None:
        """Recalcula agrupaciones semánticas tras edición/regrabación inline.

        Mantiene el grafo compilado y los interpretados; sólo refresca ``action_groups``,
        títulos, resúmenes y nivel de grupo.
        """
        compiled = mission.compiled_execution_graph
        interp = list(mission.interpreted_steps or [])
        if not compiled:
            mission.action_groups = []
            return
        # Alinea longitudes defensivas tras ediciones manuales o migraciones.
        from app.contracts.mission import InterpretedStep

        while len(interp) < len(compiled):
            interp.append(
                InterpretedStep(description=f"Paso {len(interp) + 1}", raw_event_ids=[])
            )
        interp = interp[: len(compiled)]
        mission.action_groups = MissionCompiler._build_semantic_action_groups(
            compiled, interp,
        )

    # ──────────────────────────────────────────────────────────────
    # Field Editing Sessions — captura VALOR FINAL, no historia cruda
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_text_sessions(raw_trace: List[RawEvent]) -> List[RawEvent]:
        """Colapsa tecleo-en-campo en un único evento con el texto final.

        Reglas:
        - Caracteres imprimibles con o sin `shift` pertenecen a la sesión.
        - `backspace` sin modificadores borra el último char del buffer.
        - Modificadores "reales" (ctrl/alt/win) → cierran la sesión.
        - Teclas especiales (tab/enter/esc/arrows/home/end/f1...) → cierran.
        - Cualquier evento no-teclado (click, drag, scroll) → cierra.
        - Si el buffer final queda vacío (solo borrados) → se descarta.

        Nota: NO cerramos por cambio de título de ventana. Apps web (Chrome,
        Gmail, formularios) cambian el título mientras el usuario tipea
        (autocompletado, indicadores de carga, etc.). Cerrar por eso partiría
        "Daniel Ramos" en dos pasos sin razón. El foco lo perdemos solo cuando
        hay un evento claro (click/tab/enter/hotkey), que SÍ cerramos.
        """
        if not raw_trace:
            return []

        out: List[RawEvent] = []
        buffer: List[RawEvent] = []
        buf_text: List[str] = []  # lista de chars para permitir pop() en O(1)
        anchor_ev: Optional[RawEvent] = None

        def _is_text_like(ev: RawEvent) -> Tuple[bool, bool, bool]:
            """Devuelve (is_text, is_backspace, is_capslock_toggle).

            Solo uno puede ser True. ``is_capslock_toggle`` se usa para
            que CapsLock NO cierre la sesión — solo invierta el casing
            del buffer reconstruido.
            """
            if ev.event_type != EventType.KEYBOARD_KEY_PRESS:
                return (False, False, False)
            ka = ev.keyboard_action
            if not ka:
                return (False, False, False)
            key = (ka.keys or [""])[0] if ka.keys else ""
            mods = list(ka.modifiers or [])
            kc = _canonical_key(key)
            # backspace sin mods = corrección dentro de la sesión
            if kc == "backspace" and not mods:
                return (False, True, False)
            # CapsLock dentro de una sesión de texto: NUNCA cierra
            # — solo invierte casing del buffer. Bug 2026-05-03: el
            # listener antiguo emitía CapsLock como KEY_PRESS y eso
            # partía "Chrome" en "C" + "hrome". El listener actual ya
            # absorbe CapsLock, pero defendemos en profundidad por si
            # llega de una grabación legacy.
            if kc in ("capslock", "caps_lock"):
                return (False, False, True)
            # cualquier modificador que NO sea solo shift ⇒ hotkey, no texto
            non_shift_mods = [m for m in mods if m != "shift"]
            if non_shift_mods:
                return (False, False, False)
            # carácter imprimible (1 char) sin modificadores reales
            if len(key) == 1 and not _is_special_key(key):
                return (True, False, False)
            # "space" normalizada (pynput puede entregar "space" en algunos backends)
            if kc == "space":
                return (True, False, False)
            return (False, False, False)

        # Estado de CapsLock acumulado dentro de la sesión. Se inicia
        # leyendo ``metadata.capslock_on`` del primer evento (que el
        # KeyboardListener ya marca con el estado actual). Si no hay
        # metadata (grabación legacy), asumimos OFF.
        capslock_on_local: List[bool] = [False]

        def _apply_text(ev: RawEvent) -> None:
            ka = ev.keyboard_action
            if not ka:
                return
            # Sincronizamos con el estado capturado por el listener
            # (el listener moderno ya inyecta ``capslock_on`` en metadata).
            try:
                cs = (ev.metadata or {}).get("capslock_on")
                if cs is not None:
                    capslock_on_local[0] = bool(cs)
            except Exception:
                pass
            raw = (ka.keys or [""])[0]
            mods = list(ka.modifiers or [])
            kc = _canonical_key(raw)
            if kc == "space" or raw == " ":
                buf_text.append(" ")
                return
            if len(raw) == 1:
                # Casing combinado de shift + capslock para letras.
                # CapsLock solo afecta letras alfabéticas; números y
                # símbolos siguen reglas estándar de shift.
                ch = raw
                if raw.isalpha():
                    upper = ("shift" in mods) ^ capslock_on_local[0]
                    ch = raw.upper() if upper else raw.lower()
                else:
                    ch = raw.upper() if "shift" in mods else raw
                buf_text.append(ch)

        def flush() -> None:
            nonlocal anchor_ev
            if not buffer:
                anchor_ev = None
                return
            final = "".join(buf_text)
            if final:
                # Evento sintético que el _compile_group reconocerá por
                # metadata["field_session"]. Usamos KEYBOARD_TYPE_TEXT para
                # diferenciarlo de KEYBOARD_KEY_PRESS (que siguen siendo
                # hotkeys / teclas especiales individuales).
                try:
                    synth = RawEvent(
                        id=f"fs_{buffer[0].id}",
                        event_type=EventType.KEYBOARD_TYPE_TEXT,
                        keyboard_action=None,
                        window_context=anchor_ev.window_context if anchor_ev else None,
                        timestamp=anchor_ev.timestamp if anchor_ev else buffer[0].timestamp,
                        metadata={
                            "field_session": True,
                            "final_text": final,
                            "source_event_ids": [e.id for e in buffer],
                        },
                        snapshot_ref=anchor_ev.snapshot_ref if anchor_ev else None,
                    )
                    out.append(synth)
                except Exception:
                    # En caso de validación fallida, caemos a emitir los eventos crudos
                    # (mejor conservar datos que perderlos).
                    out.extend(buffer)
            # else: buffer completo fue corrección (p.ej. abrió sesión y borró todo)
            buffer.clear()
            buf_text.clear()
            anchor_ev = None

        for ev in raw_trace:
            is_text, is_bs, is_caps = _is_text_like(ev)

            if is_text:
                if not buffer:
                    anchor_ev = ev
                buffer.append(ev)
                _apply_text(ev)
                continue

            if is_bs:
                if buffer:
                    buffer.append(ev)
                    if buf_text:
                        buf_text.pop()
                    continue
                # backspace "aislado" (sin sesión previa) → pasa como evento normal
                out.append(ev)
                continue

            if is_caps:
                # CapsLock dentro O fuera de sesión: NO se reenvía como
                # evento visible. Solo alterna el estado local para que
                # las próximas teclas reflejen el casing correcto.
                capslock_on_local[0] = not capslock_on_local[0]
                continue

            # Cualquier otro evento cierra la sesión y se reenvía tal cual.
            if buffer:
                flush()
            out.append(ev)

        flush()
        return out

    # ──────────────────────────────────────────────────────────────
    # Aggregation
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _aggregate_events(raw_trace: List[RawEvent]) -> List[List[RawEvent]]:
        """Agrupa eventos crudos:
        - Consecutivos caracteres normales (sin modifiers) → 1 grupo TYPE_TEXT.
        - Teclas especiales idénticas consecutivas (misma key, mismos mods) → 1 grupo.
        - Cualquier otra tecla / hotkey → su propio grupo.
        - Clicks / scroll / drag → su propio grupo.
        """
        if not raw_trace:
            return []

        grouped: List[List[RawEvent]] = []
        current: List[RawEvent] = []
        current_kind: str = ""

        def _kb_key(ev: RawEvent) -> str:
            try:
                return _canonical_key((ev.keyboard_action.keys or [""])[0])
            except Exception:
                return ""

        def _kb_mods(ev: RawEvent) -> Tuple[str, ...]:
            try:
                return tuple(sorted(ev.keyboard_action.modifiers or []))
            except Exception:
                return tuple()

        def _kb_kind(ev: RawEvent) -> str:
            k = _kb_key(ev)
            mods = _kb_mods(ev)
            if mods:
                return f"hotkey:{mods}:{k}"
            # "space" sin modificadores es parte del texto tipeado, no una tecla especial
            if k == "space":
                return "text"
            if _is_special_key(k):
                return f"special:{k}"
            return "text"

        for ev in raw_trace:
            # Los eventos sintéticos de Field Editing Session (KEYBOARD_TYPE_TEXT
            # con metadata["field_session"]=True) son siempre su propio grupo:
            # ya contienen el valor final compilado.
            if ev.event_type == EventType.KEYBOARD_TYPE_TEXT:
                if current:
                    grouped.append(current)
                    current = []
                    current_kind = ""
                grouped.append([ev])
                continue

            if ev.event_type == EventType.KEYBOARD_KEY_PRESS:
                kind = _kb_kind(ev)
                if kind == "text" and current_kind == "text":
                    current.append(ev)
                elif kind == current_kind and kind.startswith("special:"):
                    current.append(ev)
                elif kind == current_kind and kind.startswith("hotkey:"):
                    # Hotkey idéntica repetida también se agrupa
                    current.append(ev)
                else:
                    if current:
                        grouped.append(current)
                    current = [ev]
                    current_kind = kind
            else:
                if current:
                    grouped.append(current)
                    current = []
                    current_kind = ""
                grouped.append([ev])

        if current:
            grouped.append(current)
        return grouped

    # ──────────────────────────────────────────────────────────────
    # Per-group compilation
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _compile_group(group: List[RawEvent], mission: Mission) -> Tuple[Optional[CompiledStep], Optional[InterpretedStep]]:
        first = group[0]
        i_step = InterpretedStep(
            id=str(uuid.uuid4()),
            description="",
            raw_event_ids=[e.id for e in group],
        )
        c_step = CompiledStep(id=str(uuid.uuid4()))

        # ── window hint (para todos) ───────────────────────────────
        wctx = first.window_context
        window_hint: Dict[str, Any] = {}
        if wctx is not None:
            window_hint = {
                "title": getattr(wctx, "title", "") or "",
                "bbox": getattr(wctx, "bounding_box", None) or {},
                "pid": getattr(wctx, "hwnd", None),
                "process_name": getattr(wctx, "process_name", "") or "",
            }

        # ── snapshot/uia compartidos + TargetBundle rico ──────────
        uia_meta = first.metadata.get("uia") if first.metadata else None
        has_snapshot = bool(first.snapshot_ref)
        snap_mid = (first.metadata or {}).get("snapshot_mid")
        anchor_bbox = (first.metadata or {}).get("anchor_bbox")
        click_offset = (first.metadata or {}).get("click_offset")
        rel_win = (first.metadata or {}).get("rel_win")

        c_step.target_context = TargetContext()
        c_step.target_context.image_ref = first.snapshot_ref
        if snap_mid:
            c_step.target_context.image_ref_mid = snap_mid
        if anchor_bbox and isinstance(anchor_bbox, dict):
            try:
                c_step.target_context.anchor_bbox = {
                    "left": int(anchor_bbox.get("left", 0)),
                    "top": int(anchor_bbox.get("top", 0)),
                    "width": int(anchor_bbox.get("width", 0)),
                    "height": int(anchor_bbox.get("height", 0)),
                }
            except Exception:
                pass
        if click_offset and isinstance(click_offset, dict):
            try:
                c_step.target_context.click_offset_within_bbox = {
                    "dx": int(click_offset.get("dx", 0)),
                    "dy": int(click_offset.get("dy", 0)),
                }
            except Exception:
                pass
        if rel_win and isinstance(rel_win, dict):
            try:
                c_step.target_context.fallback_coords_relative_to_window = {
                    "fx": float(rel_win.get("fx", 0.0)),
                    "fy": float(rel_win.get("fy", 0.0)),
                }
            except Exception:
                pass
        if wctx is not None:
            c_step.target_context.hwnd = getattr(wctx, "hwnd", None)
            c_step.target_context.window_title = getattr(wctx, "title", "") or None
            c_step.target_context.process_name = (
                getattr(wctx, "process_name", "") or None
            )

        # Web data (Playwright locators) — viaja desde el recorder en
        # metadata["web"]. Si está, queda como primera fuente de verdad
        # para el TargetResolver y el SET_FIELD_VALUE/CLICK web.
        web_meta = (first.metadata or {}).get("web") if first.metadata else None
        if web_meta and isinstance(web_meta, dict):
            c_step.target_context.web_data = web_meta

        # PRD 2026-05-12: visual_fingerprint del grabador (pHash/dHash/ORB)
        # viaja como sub-dict dentro de ``vision_data``. El
        # ``TargetResolver._try_visual_fingerprint`` lo lee para
        # re-localizar elementos movidos sin tener que regrabar.
        vfp_meta = (first.metadata or {}).get("visual_fingerprint") \
            if first.metadata else None
        if isinstance(vfp_meta, dict) and vfp_meta.get("available"):
            vision_data = dict(c_step.target_context.vision_data or {})
            vision_data["visual_fingerprint"] = dict(vfp_meta)
            c_step.target_context.vision_data = vision_data

        sig_blob = (first.metadata or {}).get("target_signature") if first.metadata else None
        if isinstance(sig_blob, dict):
            merged_sig = dict(sig_blob)
            iso = (
                (first.metadata or {}).get("target_identity_isolation") or {}
            )
            if iso:
                merged_sig["trusted_pre_action_identity"] = bool(
                    iso.get("trusted_pre_action_identity")
                )
                merged_sig["successful_state_transition"] = bool(
                    iso.get("successful_state_transition")
                )
                merged_sig["identity_preserved_after_transition"] = bool(
                    iso.get("identity_preserved_after_transition")
                )
                dr = iso.get("degradation_reason")
                if dr:
                    merged_sig["degradation_reason"] = dr
            c_step.target_context.signature = merged_sig

        # ICON_VALIDATED_COORDS solo se activa cuando tenemos el bundle
        # COMPLETO (anchor_bbox + snapshot_mid + click_offset). Sin ese
        # set completo, el sanity-check del player no puede funcionar bien
        # y es mejor usar el camino probado UIA_THEN_VISION.
        has_full_bundle = bool(snap_mid and anchor_bbox and click_offset)
        has_visual_anchor = bool(snap_mid or has_snapshot)

        if uia_meta:
            merged = dict(uia_meta)
            if window_hint and "window" not in merged:
                merged["window"] = window_hint
            c_step.target_context.uia_data = merged
            if has_full_bundle:
                # COORDINATE_ICON_VALIDATED_CLICK = pipeline premium coords+icon+validación regional.
                c_step.target_resolution_strategy = (
                    TargetResolutionStrategy.COORDINATE_ICON_VALIDATED_CLICK
                )
            else:
                # Camino probado de siempre: UIA → visión → fallback coords.
                c_step.target_resolution_strategy = TargetResolutionStrategy.UIA_THEN_VISION
        elif window_hint.get("title"):
            c_step.target_context.uia_data = {"window": window_hint}
            if has_full_bundle:
                c_step.target_resolution_strategy = (
                    TargetResolutionStrategy.COORDINATE_ICON_VALIDATED_CLICK
                )
            elif has_visual_anchor:
                c_step.target_resolution_strategy = TargetResolutionStrategy.VISION_STRICT
            else:
                c_step.target_resolution_strategy = TargetResolutionStrategy.COORDS_ABSOLUTE
        elif has_snapshot:
            c_step.target_resolution_strategy = TargetResolutionStrategy.VISION_STRICT
        else:
            c_step.target_resolution_strategy = TargetResolutionStrategy.COORDS_ABSOLUTE

        if first.mouse_action:
            c_step.target_context.fallback_coords = {
                "x": int(first.mouse_action.x), "y": int(first.mouse_action.y)
            }
            c_step.fallback_strategy = FallbackStrategy.USE_COORDS

        et = first.event_type

        # ── MOUSE: click / dbl / right ─────────────────────────────
        if et in (EventType.MOUSE_CLICK, EventType.MOUSE_DOUBLE_CLICK, EventType.MOUSE_RIGHT_CLICK):
            if et == EventType.MOUSE_CLICK:
                c_step.action_strategy = ActionStrategy.CLICK
                verb = "Click"
            elif et == EventType.MOUSE_DOUBLE_CLICK:
                c_step.action_strategy = ActionStrategy.DOUBLE_CLICK
                verb = "Doble-click"
            else:
                c_step.action_strategy = ActionStrategy.RIGHT_CLICK
                verb = "Click derecho"

            label = MissionCompiler._pick_target_label(uia_meta)
            if label:
                i_step.description = f"{verb} en '{label}'"
            elif has_snapshot:
                i_step.description = f"{verb} (referencia visual)"
            else:
                mx = c_step.target_context.fallback_coords or {}
                i_step.description = f"{verb} en ({mx.get('x','?')},{mx.get('y','?')})"

        # ── MOUSE: drag ────────────────────────────────────────────
        elif et == EventType.MOUSE_DRAG:
            da = first.drag_action
            if not da:
                return None, None
            c_step.action_strategy = ActionStrategy.DRAG_DROP
            c_step.action_payload = {
                "start_x": int(da.start_x), "start_y": int(da.start_y),
                "end_x": int(da.end_x), "end_y": int(da.end_y),
                "duration": float(da.duration or 0.3),
            }
            i_step.description = f"🖱 Arrastrar de ({da.start_x},{da.start_y}) a ({da.end_x},{da.end_y})"

        # ── MOUSE: scroll ─────────────────────────────────────────
        elif et == EventType.MOUSE_SCROLL:
            meta = first.metadata.get("scroll") if first.metadata else None
            dy = int((meta or {}).get("dy", 0))
            dx = int((meta or {}).get("dx", 0))
            c_step.action_strategy = ActionStrategy.SCROLL
            c_step.action_payload = {
                "dy": dy,
                "dx": dx,
                "x": first.mouse_action.x if first.mouse_action else None,
                "y": first.mouse_action.y if first.mouse_action else None,
            }
            direction = "abajo" if dy < 0 else ("arriba" if dy > 0 else "lateral")
            magnitude = abs(dy) if dy != 0 else abs(dx)
            i_step.description = f"🖲 Scroll hacia {direction} ({magnitude} ticks)"

        # ── FIELD EDITING SESSION (valor final compilado) ─────────
        elif et == EventType.KEYBOARD_TYPE_TEXT and (first.metadata or {}).get("field_session"):
            final_text = str(first.metadata.get("final_text") or "")

            # Defensa de profundidad contra contaminación por URL.
            # Si el ``final_text`` parece URL Y el ``user_typed_text``
            # del field_commit dice otra cosa (o nada), preferir lo
            # tipeado por el usuario. Cubre el bug 2026-05-03 donde
            # ``page.url`` se filtraba como texto del campo. En el
            # 99% de los casos el recorder ya saneó esto en
            # ``_build_field_commit_result``, pero defendemos aquí
            # contra grabaciones legacy o paths nuevos.
            try:
                from app.services.missions.field_commit import (
                    is_url_contamination,
                )
                fc = (first.metadata or {}).get("field_commit") or {}
                user_typed = str(fc.get("user_typed_text") or "")
                if is_url_contamination(
                    read_value=final_text, user_typed=user_typed,
                ):
                    final_text = user_typed
            except Exception:
                pass

            c_step.action_strategy = ActionStrategy.TYPE_TEXT
            rs = str((first.metadata or {}).get("committed_read_source") or "").strip()
            flabel = str((first.metadata or {}).get("field_label") or "").strip()
            c_step.action_payload = {
                "text": final_text,
                "field_label": flabel,
                "field_session": True,
                "committed_read_source": rs,
            }
            show = final_text if len(final_text) <= 40 else final_text[:40] + "…"
            if flabel:
                i_step.description = f"⌨ Rellenar campo '{flabel}' con '{show}'"
            else:
                i_step.description = f"⌨ Escribir valor final '{show}'"

        # ── KEYBOARD ──────────────────────────────────────────────
        elif et == EventType.KEYBOARD_KEY_PRESS:
            mods = list(first.keyboard_action.modifiers or []) if first.keyboard_action else []
            first_key = _canonical_key(
                (first.keyboard_action.keys or [""])[0] if first.keyboard_action else ""
            )
            repeat = len(group)

            if mods:
                # Hotkey con modificadores — siempre como COMBO simultáneo
                c_step.action_strategy = ActionStrategy.SEND_HOTKEY
                if repeat > 1:
                    c_step.action_payload = {
                        "combo": True,
                        "keys": list(mods) + [first_key],
                        "repeat": repeat,
                    }
                else:
                    c_step.action_payload = {
                        "combo": True,
                        "keys": list(mods) + [first_key],
                    }
                i_step.description = _describe_hotkey(mods, first_key, repeat)
            elif _is_special_key(first_key):
                c_step.action_strategy = ActionStrategy.SEND_HOTKEY
                if repeat > 1:
                    c_step.action_payload = {"key": first_key, "repeat": repeat}
                else:
                    c_step.action_payload = {"hotkey": first_key}
                i_step.description = _describe_hotkey([], first_key, repeat)
            else:
                c_step.action_strategy = ActionStrategy.TYPE_TEXT
                def _ch(e: RawEvent) -> str:
                    raw = (e.keyboard_action.keys or [""])[0] if e.keyboard_action else ""
                    # Preserve spaces (both " " literal and pynput's "space")
                    if raw == " " or raw.lower() == "space":
                        return " "
                    k = _canonical_key(raw)
                    return k if len(k) == 1 else ""
                text = "".join(_ch(e) for e in group)
                c_step.action_payload = {"text": text}
                show = text if len(text) <= 40 else text[:40] + "…"
                i_step.description = f"⌨ Escribir '{show}'"
        else:
            return None, None

        # ── User annotations ──────────────────────────────────────
        for ann in mission.user_annotations:
            if ann.target_step_id in i_step.raw_event_ids:
                if ann.annotation_type.value == "fallback":
                    c_step.fallback_strategy = FallbackStrategy.ASK_HUMAN
                    i_step.description += f" [Fallback: {ann.value}]"
                elif ann.annotation_type.value == "rule":
                    if c_step.action_strategy == ActionStrategy.TYPE_TEXT:
                        c_step.action_strategy = ActionStrategy.AGENT_PROMPT
                        orig = c_step.action_payload.get("text", "")
                        c_step.action_payload = {
                            "text": (
                                f"Escribe con teclado lo que cumpla esta regla: "
                                f"'{ann.value}'. (El texto original de la grabación era: {orig})"
                            )
                        }
                        i_step.description = f"🤖 Dinámico: {ann.value} (Original: {orig})"
                    else:
                        i_step.description += f" [Regla: {ann.value}]"
                elif ann.annotation_type.value == "verification":
                    c_step.validation_strategy = ValidationStrategy.REQUIRE_IMAGE_MATCH
                    i_step.description += f" [Validar: {ann.value}]"

        return c_step, i_step

    # ──────────────────────────────────────────────────────────────
    @staticmethod
    def _pick_target_label(uia_meta: Optional[Dict[str, Any]]) -> str:
        if not uia_meta:
            return ""
        _GENERIC = {
            "groupcontrol", "panecontrol", "customcontrol",
            "documentcontrol", "windowcontrol", "toolbarcontrol",
            "statusbarcontrol", "group", "pane", "custom",
            "document", "window", "toolbar", "statusbar",
        }
        name = (uia_meta.get("name") or "").strip()
        aid = (uia_meta.get("automation_id") or "").strip()
        ct = (uia_meta.get("control_type") or "").strip()
        if name.lower() in _GENERIC or (ct and name.lower() == ct.lower()):
            name = ""
        return name or aid

    # ──────────────────────────────────────────────────────────────
    # Post-pass: detección de patrones semánticos
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _post_pass_patterns(
        compiled: List[CompiledStep], interp: List[InterpretedStep]
    ) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
        """Detecta y enriquece patrones como "llenar campo" o "elegir menú".

        Cambio clave (TargetBundle 2.0): cuando detectamos CLICK sobre un
        EditControl seguido de TYPE_TEXT, los FUSIONAMOS en un único
        CompiledStep con action_strategy=SET_FIELD_VALUE. El player resolverá
        el target una sola vez y aplicará ValuePattern.SetValue (atómico,
        instantáneo) — fallback a paste, fallback a typewrite.
        """
        if not compiled:
            return compiled, interp

        # ── 0) Pre-fusión: TYPE_TEXT consecutivos → un único TYPE_TEXT ──
        # Casos reales: el recorder a veces emite varios TYPE_TEXT seguidos
        # cuando hay pausas o cambios de modificadores. Los unificamos para
        # que la fusión posterior CLICK+TYPE_TEXT sea limpia.
        merged_c: List[CompiledStep] = []
        merged_i: List[InterpretedStep] = []
        j = 0
        while j < len(compiled):
            cs = compiled[j]
            ii = interp[j] if j < len(interp) else None
            if cs.action_strategy == ActionStrategy.TYPE_TEXT:
                buf = str((cs.action_payload or {}).get("text", ""))
                ids = list(ii.raw_event_ids) if ii else []
                k = j + 1
                while k < len(compiled) and compiled[k].action_strategy == ActionStrategy.TYPE_TEXT:
                    buf += str((compiled[k].action_payload or {}).get("text", ""))
                    if k < len(interp):
                        ids += list(interp[k].raw_event_ids)
                    k += 1
                cs.action_payload = {"text": buf}
                if ii is not None:
                    ii.raw_event_ids = ids
                    short = buf if len(buf) <= 40 else buf[:40] + "…"
                    ii.description = f"⌨️ Escribir '{short}'"
                merged_c.append(cs)
                if ii is not None:
                    merged_i.append(ii)
                j = k
                continue
            merged_c.append(cs)
            if ii is not None:
                merged_i.append(ii)
            j += 1
        compiled = merged_c
        interp = merged_i

        # ── 1) Fusión CLICK + TYPE_TEXT → SET_FIELD_VALUE ─────────────
        # Filosofía: si el usuario hizo CLICK y a continuación escribió,
        # la INTENCIÓN es "rellenar ese campo con ese texto", aunque el
        # ControlType o el rol web no estén bien expuestos (caso típico
        # en webs modernas, contenteditable, custom inputs, etc.).
        #
        # Fusionamos siempre que haya CLICK + TYPE_TEXT consecutivos.
        # El player guardará un control_type_hint/web_role_hint para
        # decidir SetValue / fill / paste / typewrite.
        fused_c: List[CompiledStep] = []
        fused_i: List[InterpretedStep] = []
        i = 0
        n = len(compiled)
        EDIT_CT = {"editcontrol", "edit", "documentcontrol",
                   "passwordcontrol", "comboboxcontrol"}
        # Controles claramente NO editables — clic en ellos seguido de texto
        # NO debe fusionarse (ej: clic en "Submit" que abre modal con campo).
        BUTTON_CT = {"buttoncontrol", "hyperlinkcontrol", "menuitemcontrol",
                     "checkboxcontrol", "radiobuttoncontrol", "tabitemcontrol",
                     "treeitemcontrol", "listitemcontrol"}
        WEB_EDIT_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
        WEB_EDIT_TAGS = {"input", "textarea", "select"}
        WEB_BUTTON_ROLES = {"button", "link", "menuitem", "tab",
                            "checkbox", "radio", "switch"}
        WEB_BUTTON_TAGS = {"button", "a"}
        while i < n:
            step = compiled[i]
            istep = interp[i] if i < len(interp) else None

            click_then_type = (
                step.action_strategy == ActionStrategy.CLICK
                and i + 1 < n
                and compiled[i + 1].action_strategy == ActionStrategy.TYPE_TEXT
            )

            if click_then_type:
                ct = (step.target_context.uia_data or {}).get("control_type", "") or ""
                ct = ct.lower()
                web = step.target_context.web_data or {}
                web_role = (web.get("role") or "").lower()
                web_tag = (web.get("tag") or "").lower()

                is_button_like = (
                    ct in BUTTON_CT
                    or web_role in WEB_BUTTON_ROLES
                    or (web_tag in WEB_BUTTON_TAGS and web_role not in WEB_EDIT_ROLES)
                )
                strong_hint = (
                    ct in EDIT_CT
                    or web_role in WEB_EDIT_ROLES
                    or web_tag in WEB_EDIT_TAGS
                    or bool(web.get("placeholder"))
                    or bool(web.get("label"))
                )

                # Reglas:
                # - Si es claramente botón/link → NO fusionar (preserva intención).
                # - Si hay hint fuerte → fusionar con confianza "strong".
                # - Si no hay hint pero tampoco es botón → fusionar con
                #   confianza "weak" (asumimos que el clic enfocó un campo
                #   no expuesto vía accesibilidad — caso típico web custom).
                should_fuse = (not is_button_like) and (
                    strong_hint or (ct == "" and web_role == "" and web_tag == "")
                )
                if not should_fuse:
                    fused_c.append(step)
                    if istep is not None:
                        fused_i.append(istep)
                    i += 1
                    continue

                next_step = compiled[i + 1]
                next_i = interp[i + 1] if i + 1 < len(interp) else None
                txt = (next_step.action_payload or {}).get("text", "")
                short = txt if len(txt) <= 40 else txt[:40] + "…"

                label = MissionCompiler._pick_target_label(step.target_context.uia_data)
                if not label and web:
                    label = (web.get("accessible_name") or web.get("label")
                             or web.get("placeholder") or web.get("test_id")
                             or web.get("name_attr") or "")

                step.action_strategy = ActionStrategy.SET_FIELD_VALUE
                step.action_payload = {
                    "text": txt,
                    "label": label or "",
                    "control_type_hint": ct,
                    "web_role_hint": web_role,
                    "web_tag_hint": web_tag,
                    "fusion_confidence": "strong" if strong_hint else "weak",
                }
                step.validation_strategy = ValidationStrategy.REQUIRE_FIELD_VALUE
                if istep is not None:
                    pretty_label = f"'{label}'" if label else "campo"
                    istep.description = f"📝 Rellenar {pretty_label} con '{short}'"
                    if next_i is not None:
                        istep.raw_event_ids = list(istep.raw_event_ids) + list(next_i.raw_event_ids)

                fused_c.append(step)
                if istep is not None:
                    fused_i.append(istep)
                i += 2
                continue

            fused_c.append(step)
            if istep is not None:
                fused_i.append(istep)
            i += 1

        compiled = fused_c
        interp = fused_i

        for i, step in enumerate(compiled):
            # 2) Patrón: click en Menu/MenuItem + click en otro MenuItem → "Elegir menú"
            if (
                step.action_strategy == ActionStrategy.CLICK
                and step.target_context.uia_data
                and (step.target_context.uia_data.get("control_type") or "").lower()
                    in ("menucontrol", "menuitemcontrol", "menuitem", "menu")
                and i + 1 < len(compiled)
                and compiled[i + 1].action_strategy == ActionStrategy.CLICK
            ):
                first_label = MissionCompiler._pick_target_label(step.target_context.uia_data)
                second_label = MissionCompiler._pick_target_label(
                    compiled[i + 1].target_context.uia_data
                )
                if first_label and second_label:
                    interp[i].description = f"📂 Abrir menú '{first_label}'"
                    interp[i + 1].description = f"📂 Elegir '{second_label}' en '{first_label}'"

        return compiled, interp

    # ──────────────────────────────────────────────────────────────
    # Post-pass V3: patrones semánticos extra (PRD Fase 4)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _post_pass_patterns_v3(
        compiled: List[CompiledStep], interp: List[InterpretedStep]
    ) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
        """Detecta patrones de alto nivel para limpiar la misión.

        Patrones detectados:
          A) Ctrl+L (browser) + TYPE_TEXT + Enter → NAVIGATE_OR_SEARCH
          B) SET_FIELD_VALUE + Enter             → SUBMIT_SEARCH (último Enter
             se fusiona con la confirmación)
          C) CLICK menu/menuitem + CLICK menuitem → OPEN_MENU_ITEM
          D) SCROLL + CLICK target              → SCROLL_UNTIL_VISIBLE + CLICK
             (el scroll deja un breadcrumb anchor del CLICK siguiente)
          E) ESC standalone                     → CONFIRM_DIALOG (cancel)
          F) Enter standalone justo tras un modal → CONFIRM_DIALOG (accept)

        Estos patrones son OPORTUNISTAS: si hay duda, dejamos los pasos
        sueltos (es mejor un paso de más que perder un detalle).
        """
        if not compiled:
            return compiled, interp

        out_c: List[CompiledStep] = []
        out_i: List[InterpretedStep] = []
        i = 0
        n = len(compiled)

        def _is_hotkey(step: CompiledStep, mods: Tuple[str, ...], key: str) -> bool:
            if step.action_strategy != ActionStrategy.SEND_HOTKEY:
                return False
            payload = step.action_payload or {}
            if payload.get("combo"):
                keys = list(payload.get("keys") or [])
                if not keys:
                    return False
                k_target = (keys[-1] or "").lower()
                m_target = tuple(sorted(k.lower() for k in keys[:-1]))
                return m_target == tuple(sorted(mods)) and k_target == key
            return (mods == tuple()) and (
                (payload.get("hotkey") or payload.get("key") or "").lower() == key
            )

        while i < n:
            step = compiled[i]
            istep = interp[i] if i < len(interp) else None

            # ── A) Ctrl+L + TYPE_TEXT + Enter → NAVIGATE_OR_SEARCH ──
            # Solo aplica si el primer paso es Ctrl+L y los tres pertenecen
            # a un proceso navegador (mantenemos heurística simple: si el
            # paso siguiente es TYPE_TEXT/SET_FIELD_VALUE, fusionamos).
            if (
                _is_hotkey(step, ("ctrl",), "l")
                and i + 2 < n
                and compiled[i + 1].action_strategy in (
                    ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE
                )
                and _is_hotkey(compiled[i + 2], (), "enter")
            ):
                text_step = compiled[i + 1]
                text = (text_step.action_payload or {}).get("text", "")
                merged = step
                merged.action_strategy = ActionStrategy.NAVIGATE_OR_SEARCH
                merged.action_payload = {"text": str(text)}
                merged.target_context = step.target_context
                if istep is not None:
                    short = text if len(text) <= 60 else text[:60] + "…"
                    istep.description = f"🌐 Navegar/buscar '{short}'"
                    if i + 1 < len(interp):
                        istep.raw_event_ids = (list(istep.raw_event_ids)
                                                + list(interp[i + 1].raw_event_ids))
                    if i + 2 < len(interp):
                        istep.raw_event_ids += list(interp[i + 2].raw_event_ids)
                out_c.append(merged)
                if istep is not None:
                    out_i.append(istep)
                i += 3
                continue

            # ── B) SET_FIELD_VALUE/TYPE_TEXT + Enter → SUBMIT_SEARCH ─────────
            # Más fuerte que dejar el Enter suelto: el target del Enter
            # hereda el campo, así el resolver sabe a quién pertenece.
            # Cubrimos TYPE_TEXT (field session colapsada) además de
            # SET_FIELD_VALUE: el caso real reportado el 2026-05-03 venía
            # como TYPE_TEXT por el path del recorder.
            if (
                step.action_strategy in (
                    ActionStrategy.SET_FIELD_VALUE, ActionStrategy.TYPE_TEXT,
                )
                and i + 1 < n
                and _is_hotkey(compiled[i + 1], (), "enter")
                # Solo si tiene contexto de navegador o de campo (evita
                # fusionar TYPE_TEXT genérico que no es búsqueda):
                and (
                    step.action_strategy == ActionStrategy.SET_FIELD_VALUE
                    or (step.action_payload or {}).get("field_session")
                    or (step.action_payload or {}).get("field_label")
                    or (step.action_payload or {}).get("label")
                    or ((step.target_context.web_data or {}).get("domain")
                        if step.target_context else None)
                )
            ):
                merged_step = step
                # No modificamos action_strategy del paso fusionado
                # (queda como SET_FIELD_VALUE), pero le agregamos
                # `submit_after: True` al payload para que el player
                # presione Enter al terminar de fillear.
                payload = dict(merged_step.action_payload or {})
                payload["submit_after"] = True

                # ── Enriquecimiento por dominio web ────────────────
                # Si el campo está en YouTube/Google/etc, lo marcamos
                # para que el player priorice estrategias específicas
                # del sitio (paste rápido + Enter, validar URL final).
                # Bug 2026-05-03: una búsqueda en YouTube aparecía como
                # "Rellenar campo 'guide-service'" sin contexto humano.
                site_label = ""
                site_kind = ""
                tc = step.target_context
                web = (tc.web_data or {}) if tc else {}
                domain = (web.get("domain") or "").strip().lower()
                url = (web.get("url") or "").strip().lower()
                if "youtube.com" in domain or "youtube.com" in url:
                    site_label = "YouTube"
                    site_kind = "youtube"
                elif "google.com" in domain and "maps" not in url:
                    site_label = "Google"
                    site_kind = "google"
                elif domain:
                    # Tomamos hostname legible (ej. 'github.com' → 'GitHub')
                    parts = domain.split(".")
                    if len(parts) >= 2 and parts[-2]:
                        site_label = parts[-2].capitalize()
                        site_kind = parts[-2]

                if site_label:
                    payload["search_site"] = site_kind
                    payload["search_site_label"] = site_label

                merged_step.action_payload = payload
                if istep is not None:
                    txt = payload.get("text", "")
                    short = txt if len(txt) <= 40 else txt[:40] + "…"
                    if site_label:
                        # "Buscar en YouTube 'Devuelveme...'" — intención humana.
                        istep.description = (
                            f"🔎 Buscar en {site_label} '{short}'"
                        )
                    else:
                        label = payload.get("label") or payload.get("field_label") or ""
                        pretty_label = f"'{label}'" if label else "campo"
                        istep.description = (
                            f"📝 Rellenar {pretty_label} con '{short}' y enviar (Enter)"
                        )
                    if i + 1 < len(interp):
                        istep.raw_event_ids = (list(istep.raw_event_ids)
                                                + list(interp[i + 1].raw_event_ids))
                out_c.append(merged_step)
                if istep is not None:
                    out_i.append(istep)
                i += 2
                continue

            # ── E) ESC suelta → CONFIRM_DIALOG (cancel) ─────────────
            if (
                _is_hotkey(step, (), "esc")
                and step.action_payload
                and not (step.action_payload or {}).get("repeat")
            ):
                step.action_strategy = ActionStrategy.CONFIRM_DIALOG
                step.action_payload = {"answer": "cancel"}
                if istep is not None and not istep.description.startswith("✕"):
                    istep.description = "✕ Cancelar diálogo (Esc)"
                out_c.append(step)
                if istep is not None:
                    out_i.append(istep)
                i += 1
                continue

            out_c.append(step)
            if istep is not None:
                out_i.append(istep)
            i += 1

        # ── C) y D) se hacen en una segunda pasada simple sobre out_c ─
        compiled = out_c
        interp = out_i

        # C) CLICK menu/menuitem + CLICK menuitem → OPEN_MENU_ITEM
        # Lo enriquecemos en el descriptor pero NO fusionamos para
        # preservar la trazabilidad. Sólo cambiamos la action_strategy
        # del segundo CLICK a OPEN_MENU_ITEM y le copiamos el contexto
        # del menú raíz al payload.
        for k, st in enumerate(compiled):
            if (
                st.action_strategy == ActionStrategy.CLICK
                and st.target_context.uia_data
                and (st.target_context.uia_data.get("control_type") or "").lower()
                    in ("menucontrol", "menuitemcontrol", "menuitem", "menu",
                        "menubarcontrol")
                and k + 1 < len(compiled)
                and compiled[k + 1].action_strategy == ActionStrategy.CLICK
                and compiled[k + 1].target_context.uia_data
                and (compiled[k + 1].target_context.uia_data.get("control_type") or "").lower()
                    in ("menuitemcontrol", "menuitem", "listitemcontrol", "listitem")
            ):
                child = compiled[k + 1]
                child.action_strategy = ActionStrategy.OPEN_MENU_ITEM
                first_label = MissionCompiler._pick_target_label(st.target_context.uia_data)
                second_label = MissionCompiler._pick_target_label(child.target_context.uia_data)
                child.action_payload = dict(child.action_payload or {})
                child.action_payload["menu_root"] = first_label
                child.action_payload["menu_item"] = second_label
                if k + 1 < len(interp):
                    interp[k + 1].description = (
                        f"📂 Abrir '{second_label}' en menú '{first_label}'"
                        if first_label and second_label
                        else f"📂 Abrir item de menú"
                    )

        # D) SCROLL + CLICK → SCROLL_UNTIL_VISIBLE + CLICK_TARGET
        # No reemplazamos al CLICK, solo marcamos el SCROLL como
        # SCROLL_UNTIL_VISIBLE y le copiamos el target del CLICK como
        # `target_to_show`. El player decidirá si puede usar
        # `locator.scroll_into_view_if_needed()` y saltarse el scroll.
        for k, st in enumerate(compiled):
            if (
                st.action_strategy == ActionStrategy.SCROLL
                and k + 1 < len(compiled)
                and compiled[k + 1].action_strategy in (
                    ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK,
                    ActionStrategy.RIGHT_CLICK, ActionStrategy.SET_FIELD_VALUE,
                )
            ):
                next_step = compiled[k + 1]
                st.action_strategy = ActionStrategy.SCROLL_UNTIL_VISIBLE
                # Copiamos un descriptor mínimo del target a mostrar
                tc = next_step.target_context
                marker: Dict[str, Any] = {}
                if tc.uia_data:
                    marker["uia_name"] = (tc.uia_data.get("name") or "").strip()
                    marker["uia_aid"] = (tc.uia_data.get("automation_id") or "").strip()
                    marker["uia_ct"] = (tc.uia_data.get("control_type") or "").strip()
                if tc.web_data:
                    web = tc.web_data
                    marker["web_role"] = (web.get("role") or "").strip()
                    marker["web_name"] = (web.get("accessible_name") or "").strip()
                    marker["web_label"] = (web.get("label") or "").strip()
                    marker["web_test_id"] = (web.get("test_id") or "").strip()
                st.action_payload = dict(st.action_payload or {})
                st.action_payload["scroll_until"] = marker
                if k < len(interp):
                    label = (marker.get("uia_name") or marker.get("web_name")
                             or marker.get("web_label") or marker.get("web_test_id")
                             or "elemento")
                    interp[k].description = f"⬇ Hacer visible '{label}'"

        return compiled, interp

    # ──────────────────────────────────────────────────────────────
    # Post-pass: Windows Search → LAUNCH_APP
    # ──────────────────────────────────────────────────────────────

    # Frases EXACTAS del cuadro de Windows Search en distintos idiomas.
    # Evitamos términos genéricos ("search", "buscar") porque también
    # aparecen en cajas de búsqueda de webs/apps (Google, GitHub, etc.).
    # El process_name cubre los casos donde el UIA name es genérico.
    _WINDOWS_SEARCH_NAMES = {
        "escribe aquí para buscar",   # es-MX / es-ES
        "escriba aquí para buscar",   # variante
        "type here to search",         # en-US
        "search windows",              # variante
        "hier eingeben zum suchen",    # de-DE
        "eingabe für suche",           # variante
        "tapez ici pour rechercher",   # fr-FR
        "digite aqui para pesquisar",  # pt-BR
        "cortana",                     # Win10 viejo
    }

    _WINDOWS_SEARCH_PROCESSES = {
        "searchhost.exe", "searchui.exe", "searchapp.exe",
        "startmenuexperiencehost.exe", "cortana.exe",
    }

    @staticmethod
    def _post_pass_windows_search_launch(
        compiled: List[CompiledStep], interp: List[InterpretedStep]
    ) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
        """Detecta patrón: CLICK en Windows Search + TYPE_TEXT (+ Enter / o
        nuevo proceso target) → LAUNCH_APP.

        Cubre tres ramas:
          1. ``Search → type → Enter``  → colapso clásico.
          2. ``Search → type``          → colapso si el siguiente paso
             pertenece al proceso objetivo (Chrome/Excel/etc.); esto
             cubre "el usuario hizo click sobre el resultado" sin Enter.
          3. ``Search → type``          → colapso si la app es conocida
             (alias resuelto) aunque no haya señal de proceso, ya que
             el player puede confirmar el lanzamiento por sí mismo.
        """
        if len(compiled) < 2:
            return compiled, interp

        # Importes locales: launch_apps no depende de nada de pydantic
        # y carga incluso en CI headless.
        from app.services.missions.launch_apps import (
            resolve_app_alias, canonical_app_name,
        )

        out_c: List[CompiledStep] = []
        out_i: List[InterpretedStep] = []
        i = 0
        n = len(compiled)

        while i < n:
            step = compiled[i]
            istep = interp[i] if i < len(interp) else None

            # Detectar CLICK en Windows Search — DEBE ser estricto
            # porque este post-pass corre antes que la fusión de campos
            # (un Edit con name "Buscar" en una web NO es Windows Search).
            #
            # Cubre 3 ramas seguras:
            #   1. UIA name = frase exacta del cuadro Win Search
            #      ("Type here to search" / "Escribe aquí para buscar"…).
            #   2. process_name ∈ {searchhost.exe, …}.
            #   3. uia_name = "Search"/"Buscar" *Y* el process es uno de
            #      Windows Search (la condición 2 ya cubre eso).
            is_search_click = False
            if step.action_strategy == ActionStrategy.CLICK:
                uia = step.target_context.uia_data or {}
                uia_name = (uia.get("name") or "").strip().lower()
                proc = (step.target_context.process_name or "").strip().lower()

                if uia_name in MissionCompiler._WINDOWS_SEARCH_NAMES:
                    is_search_click = True
                elif proc in MissionCompiler._WINDOWS_SEARCH_PROCESSES:
                    is_search_click = True

            if is_search_click and i + 1 < n:
                next_step = compiled[i + 1]
                if next_step.action_strategy in (
                    ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE
                ):
                    text = str((next_step.action_payload or {}).get("text", ""))
                    text_clean = text.strip()

                    # ── ¿Vino un Enter explícito? ───────────────────
                    has_enter = False
                    consume_enter = 0
                    if i + 2 < n:
                        after = compiled[i + 2]
                        if after.action_strategy == ActionStrategy.SEND_HOTKEY:
                            payload = after.action_payload or {}
                            keys = payload.get("keys") or []
                            hk = (
                                payload.get("hotkey")
                                or payload.get("key") or ""
                            ).lower()
                            if hk == "enter" or (
                                keys and str(keys[-1]).lower() == "enter"
                            ):
                                has_enter = True
                                consume_enter = 1

                    # ── ¿El siguiente paso pertenece a un proceso que
                    # coincide con la app tipeada? Esto cubre el caso
                    # "Buscar Chrome y hacer click en el resultado".
                    process_match_idx: Optional[int] = None
                    if not has_enter and text_clean:
                        alias = resolve_app_alias(text_clean)
                        target_procs = {
                            p.lower() for p in (alias.process_names or ())
                        }
                        # Mira los próximos 1..2 pasos.
                        scan_start = i + 2
                        scan_end = min(n, i + 4)
                        for j in range(scan_start, scan_end):
                            tc = compiled[j].target_context
                            pn = (tc.process_name or "").strip().lower()
                            if pn and target_procs and pn in target_procs:
                                process_match_idx = j
                                break

                    # Permitimos colapsar si:
                    #   - había Enter explícito, o
                    #   - el alias mapea a una app conocida (chrome,
                    #     excel, word, ...), o
                    #   - el process del paso siguiente coincide.
                    alias = resolve_app_alias(text_clean) if text_clean else None
                    has_known_alias = bool(
                        alias and alias.process_names
                    )

                    should_collapse = bool(
                        text_clean and (
                            has_enter
                            or has_known_alias
                            or process_match_idx is not None
                        )
                    )

                    if should_collapse:
                        # Build LAUNCH_APP step
                        canonical = canonical_app_name(text_clean) or text_clean
                        merged = step
                        merged.action_strategy = ActionStrategy.LAUNCH_APP
                        merged.action_payload = {
                            "app_name": canonical,
                            "raw_typed": text_clean,
                            "launch_method": "windows_search",
                            "had_enter": bool(has_enter),
                            "process_confirmed": process_match_idx is not None,
                        }
                        if istep is not None:
                            istep.description = f"🚀 Abrir {canonical}"
                            if i + 1 < len(interp):
                                istep.raw_event_ids = (
                                    list(istep.raw_event_ids)
                                    + list(interp[i + 1].raw_event_ids)
                                )
                            if has_enter and i + 2 < len(interp):
                                istep.raw_event_ids += list(
                                    interp[i + 2].raw_event_ids
                                )

                        out_c.append(merged)
                        if istep is not None:
                            out_i.append(istep)
                        i += 2 + consume_enter
                        continue

            out_c.append(step)
            if istep is not None:
                out_i.append(istep)
            i += 1

        return out_c, out_i

    # ──────────────────────────────────────────────────────────────
    # Post-pass: collapse_partial_text_commits
    # ──────────────────────────────────────────────────────────────

    # Etiquetas técnicas que NO deberían mostrarse al usuario en
    # modo normal. Cuando uia.name o web.test_id matchea uno de estos
    # patrones, ``_humanize_field_label`` busca alternativa humana.
    _TECHNICAL_LABEL_PATTERNS = (
        # Containers/IDs de webs SPA muy comunes:
        "guide-service", "ytd-app", "ytd-search", "ytd-",
        "app-root", "app-shell", "react-root",
        # Genéricos sin valor humano:
        "container", "wrapper", "outer", "inner", "root",
        # IDs autogenerados (caracteres + dígitos sin espacios):
    )

    @staticmethod
    def _post_pass_collapse_partial_text(
        compiled: List[CompiledStep], interp: List[InterpretedStep]
    ) -> Tuple[List[CompiledStep], List[InterpretedStep]]:
        """Compacta TYPE_TEXT/SET_FIELD_VALUE consecutivos del mismo
        campo cuando uno es prefijo del siguiente.

        Bug 2026-05-03 (caso real reportado):
            El usuario escribió "devuelveme el amor luis miguel" pero
            la grabación quedó con SIETE pasos:
                Escribir "D"
                Escribir "Devuelveme el amor"
                Escribir "Devuelveme el amor L"
                Escribir "Devuelveme el amor Luis"
                Escribir "Devuelveme el amor Luis M"
                Confirmar Enter
                Escribir "devuelveme el amor luis miguel"

        Esto pasaba porque el listener legacy emitía CapsLock como
        evento, fragmentando la sesión de texto. Aunque el listener
        nuevo absorbe CapsLock, defendemos en profundidad: si se
        graba una secuencia con prefijos consecutivos del MISMO
        campo, conservamos solo el final.

        Reglas:
          - Mismo ``field_label`` (o ambos vacíos) Y mismo ámbito de
            ventana/proceso.
          - El siguiente texto contiene al actual como prefijo
            case-insensitive (toleramos cambios de casing por
            CapsLock fragmentado).
          - Sin clicks/hotkeys intermedios que cambien de campo.
        """
        if len(compiled) < 2:
            return compiled, interp

        out_c: List[CompiledStep] = []
        out_i: List[InterpretedStep] = []
        absorbed_meta: List[str] = []

        def _is_text_step(s: CompiledStep) -> bool:
            return s.action_strategy in (
                ActionStrategy.TYPE_TEXT, ActionStrategy.SET_FIELD_VALUE,
            )

        def _step_text(s: CompiledStep) -> str:
            return str((s.action_payload or {}).get("text", ""))

        def _step_label(s: CompiledStep) -> str:
            return str((s.action_payload or {}).get("field_label", "")).strip()

        def _same_scope(a: CompiledStep, b: CompiledStep) -> bool:
            la, lb = _step_label(a), _step_label(b)
            # Mismo label explícito → mismo campo.
            if la and lb:
                return la == lb
            # Si uno tiene label y el otro no, ser conservador y NO fusionar.
            if la or lb:
                return False
            # Sin labels: comparamos por process/window_title.
            ta = (a.target_context.process_name or "").lower()
            tb = (b.target_context.process_name or "").lower()
            return ta == tb

        def _is_prefix_case_insensitive(short: str, long_: str) -> bool:
            return (
                bool(short)
                and bool(long_)
                and len(short) < len(long_)
                and long_.lower().startswith(short.lower())
            )

        i = 0
        n = len(compiled)
        while i < n:
            cur = compiled[i]
            cur_i = interp[i] if i < len(interp) else None
            if not _is_text_step(cur):
                out_c.append(cur)
                if cur_i is not None:
                    out_i.append(cur_i)
                i += 1
                continue

            # Buscar pasos siguientes que sean texto del mismo campo
            # y donde el texto previo es prefijo del actual.
            j = i + 1
            best_idx = i
            absorbed_local: List[str] = []
            while j < n:
                nxt = compiled[j]
                if not _is_text_step(nxt):
                    break
                if not _same_scope(cur, nxt):
                    break
                ct = _step_text(compiled[best_idx])
                nt = _step_text(nxt)
                if not (_is_prefix_case_insensitive(ct, nt) or ct == nt):
                    break
                absorbed_local.append(ct)
                best_idx = j
                j += 1

            if best_idx > i:
                # Conservamos solo el último (más completo).
                kept = compiled[best_idx]
                kept_i = interp[best_idx] if best_idx < len(interp) else None
                # Guardamos rastro en metadata experto.
                pl = kept.action_payload or {}
                pl["partial_text_absorbed"] = absorbed_local
                kept.action_payload = pl
                # Unificamos raw_event_ids para no perder el linaje.
                if kept_i is not None:
                    merged_ids: List[str] = []
                    for k in range(i, best_idx + 1):
                        ii = interp[k] if k < len(interp) else None
                        if ii is not None:
                            merged_ids += list(ii.raw_event_ids)
                    # Dedup preservando orden:
                    seen = set()
                    kept_i.raw_event_ids = [
                        x for x in merged_ids
                        if not (x in seen or seen.add(x))
                    ]
                out_c.append(kept)
                if kept_i is not None:
                    out_i.append(kept_i)
                absorbed_meta.extend(absorbed_local)
                i = best_idx + 1
            else:
                out_c.append(cur)
                if cur_i is not None:
                    out_i.append(cur_i)
                i += 1

        return out_c, out_i

    # ──────────────────────────────────────────────────────────────
    # Humanizar labels técnicos
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _humanize_field_label(c_step: CompiledStep, i_step: InterpretedStep) -> None:
        """Reemplaza labels técnicos como ``guide-service`` por algo
        humano basado en placeholder, aria-label, role, o texto del
        action_payload.

        Bug 2026-05-03 (caso real):
            Nevlan grabó ``Click en "guide-service"`` cuando el usuario
            hizo click en el campo de búsqueda de YouTube. ``guide-service``
            es el nombre técnico del componente Polymer interno, no un
            label humano. La grabación tenía un ``placeholder="Buscar"``
            visible que no se usó.
        """
        tc = c_step.target_context
        if tc is None:
            return
        web = tc.web_data or {}
        uia = tc.uia_data or {}

        def _is_technical(label: str) -> bool:
            if not label:
                return False
            lo = label.strip().lower()
            for pat in MissionCompiler._TECHNICAL_LABEL_PATTERNS:
                if pat in lo:
                    return True
            # Heurística: id autogenerado (sin espacios, con guiones,
            # más de 6 caracteres) → técnico.
            if " " not in lo and "-" in lo and len(lo) > 8:
                return True
            return False

        def _humanize_from_web() -> Optional[str]:
            for key in (
                "accessible_name", "label", "placeholder",
                "aria_label", "name_attr",
            ):
                v = (web.get(key) or "").strip()
                if v and not _is_technical(v):
                    return v
            # Role como último recurso humano:
            role = (web.get("role") or "").strip().lower()
            if role in ("searchbox", "search", "textbox"):
                # Etiqueta canónica en español:
                return "Buscar"
            if role == "combobox":
                return "Selector"
            if role == "button":
                tx = (web.get("text") or "").strip()
                if tx and not _is_technical(tx):
                    return tx
            return None

        # Si el action_payload trae un field_label técnico, lo reemplazamos.
        pl = c_step.action_payload or {}
        cur_label = str(pl.get("field_label", "")).strip()
        if _is_technical(cur_label):
            new_label = _humanize_from_web()
            if new_label:
                pl["field_label"] = new_label
                pl["original_field_label"] = cur_label
                c_step.action_payload = pl
                # Refrescamos descripción humana si existe.
                txt = str(pl.get("text", ""))
                if txt and i_step is not None:
                    show = txt if len(txt) <= 40 else txt[:40] + "…"
                    i_step.description = (
                        f"⌨ Rellenar campo '{new_label}' con '{show}'"
                    )

        # Si la descripción del paso tiene "guide-service" o similar,
        # reemplazar también allí.
        if i_step is not None and i_step.description:
            for pat in MissionCompiler._TECHNICAL_LABEL_PATTERNS:
                if pat and pat in i_step.description.lower():
                    new_label = _humanize_from_web()
                    if new_label:
                        # Reemplazo simple: sustituye la frase técnica
                        # por la humana entre comillas.
                        i_step.description = i_step.description.replace(
                            pat, new_label
                        )
                    break

    # ──────────────────────────────────────────────────────────────
    # Capture score + semantic intent (TargetBundle V3)
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _enrich_semantic_and_score(c_step: CompiledStep, i_step: InterpretedStep) -> None:
        """Rellena ``target_context.semantic`` y ``target_context.confidence``
        con campos del PRD V3.

        - semantic.intent: click_button / set_field_value / select_option /
                          open_menu / wait_state / navigate / submit
        - semantic.human_label, semantic.role, semantic.action_kind,
          semantic.near_text, semantic.expected_state_after.
        - confidence.capture_score: 0-100, según calidad de descriptores.
        - confidence.success_by_method: dict vacío inicial.
        """
        tc = c_step.target_context
        action = c_step.action_strategy
        uia = tc.uia_data or {}
        web = tc.web_data or {}

        # ── Cálculo del capture_score ───────────────────────────────
        # Reglas (suma máxima ~100):
        #   +35 si web.test_id presente
        #   +30 si web.accessible_name + role presentes
        #   +20 si web.label o placeholder presentes
        #   +30 si UIA AutomationId presente y no genérico
        #   +20 si UIA Name + ControlType (no genérico)
        #   +15 si hay anchor_bbox + image_ref_mid
        #   +10 si fallback_coords_relative_to_window presente
        #   -25 si web.low_confidence True
        #   -10 si la única señal es fallback_coords absolutas
        score = 0.0
        reasons: List[str] = []

        test_id = (web.get("test_id") or "").strip()
        web_name = (web.get("accessible_name") or "").strip()
        web_role = (web.get("role") or "").strip()
        web_label = (web.get("label") or "").strip()
        web_placeholder = (web.get("placeholder") or "").strip()
        web_low = bool(web.get("low_confidence"))

        if test_id:
            score += 35
            reasons.append("web.test_id")
        if web_name and web_role:
            score += 30
            reasons.append("web.role+name")
        if web_label or web_placeholder:
            score += 15
            reasons.append("web.label/placeholder")

        aid = (uia.get("automation_id") or "").strip()
        ct = (uia.get("control_type") or "").strip().lower()
        u_name = (uia.get("name") or "").strip()
        _GENERIC = {"groupcontrol", "panecontrol", "customcontrol",
                    "documentcontrol", "windowcontrol", "toolbarcontrol",
                    "statusbarcontrol"}
        if aid:
            score += 30
            reasons.append("uia.aid")
        if u_name and ct and ct not in _GENERIC:
            score += 20
            reasons.append("uia.name+ct")

        if tc.anchor_bbox and tc.image_ref_mid:
            score += 15
            reasons.append("vision.anchor")

        if tc.fallback_coords_relative_to_window:
            score += 10
            reasons.append("rel_win")

        if web_low:
            score -= 25
            reasons.append("-web.low_confidence")

        # Solo coords absolutas como única señal: muy frágil.
        if (not test_id and not web_name and not aid and not u_name
                and not tc.anchor_bbox):
            score -= 10
            reasons.append("-only_abs_coords")

        score = max(0.0, min(100.0, score))
        ql = _mission_step_quality(score)

        conf = dict(tc.confidence or {})
        conf["capture_score"] = round(score, 1)
        conf["capture_reasons"] = reasons
        conf["quality_level"] = ql
        conf.setdefault("success_by_method", {})
        conf.setdefault("alternate_streaks", {})
        tc.confidence = conf

        try:
            i_step.confidence_score_pct = float(conf["capture_score"])
            i_step.quality_level = ql
            i_step.confidence = max(0.0, min(1.0, float(score) / 100.0))
        except Exception:
            pass

        # Unifica señales (firma, coords solas, validación…) en un solo nivel.
        sq = compute_step_quality(c_step, i_step)
        conf2 = dict(tc.confidence or {})
        conf2["quality_level"] = sq.level
        conf2["quality_reasons"] = sq.reasons
        tc.confidence = conf2
        try:
            i_step.quality_level = sq.level
            i_step.confidence_score_pct = float(sq.capture_score_pct)
        except Exception:
            pass

        # ── Bloque semantic ─────────────────────────────────────────
        intent_map = {
            ActionStrategy.CLICK: "click_button",
            ActionStrategy.DOUBLE_CLICK: "click_button",
            ActionStrategy.RIGHT_CLICK: "click_context",
            ActionStrategy.SET_FIELD_VALUE: "set_field_value",
            ActionStrategy.TYPE_TEXT: "type_text",
            ActionStrategy.SEND_HOTKEY: "hotkey",
            ActionStrategy.SCROLL: "scroll",
            ActionStrategy.SCROLL_UNTIL_VISIBLE: "scroll_until_visible",
            ActionStrategy.SELECT_OPTION: "select_option",
            ActionStrategy.OPEN_MENU_ITEM: "open_menu_item",
            ActionStrategy.CONFIRM_DIALOG: "confirm_dialog",
            ActionStrategy.NAVIGATE_OR_SEARCH: "navigate",
            ActionStrategy.SUBMIT_SEARCH: "submit_search",
            ActionStrategy.LAUNCH_APP: "launch_app",
            ActionStrategy.DRAG_DROP: "drag_drop",
        }
        intent = intent_map.get(action, "unknown")

        human_label = (
            test_id or web_name or web_label or web_placeholder
            or u_name or aid
            or (c_step.action_payload or {}).get("label", "")
            or ""
        )
        role = web_role or (uia.get("control_type") or "")
        near_text = (web.get("nearby_text") or "")[:200]

        # expected_state_after: heurística por intent
        expected_after = ""
        if intent in ("set_field_value", "type_text"):
            expected_after = "field_value_set"
        elif intent in ("submit_search", "navigate"):
            expected_after = "url_changed"
        elif intent == "open_menu_item":
            expected_after = "menu_dismissed"
        elif intent == "confirm_dialog":
            expected_after = "dialog_closed"
        elif intent in ("click_button", "click_context"):
            expected_after = "element_state_changed"
        elif intent == "scroll_until_visible":
            expected_after = "target_visible"
        elif intent == "launch_app":
            expected_after = "app_window_opened"

        sem = dict(tc.semantic or {})
        sem.setdefault("intent", intent)
        if human_label:
            sem.setdefault("human_label", human_label)
        if role:
            sem.setdefault("role", role)
        if near_text:
            sem.setdefault("near_text", near_text)
        if expected_after:
            sem.setdefault("expected_state_after", expected_after)
        sem.setdefault("action_kind", action.value)
        tc.semantic = sem

    # ──────────────────────────────────────────────────────────────
    # Normalización / validación por paso
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_step(c_step: CompiledStep, i_step: InterpretedStep) -> None:
        """Garantiza que cada paso tenga payload/target coherente y descripción."""
        try:
            payload = dict(c_step.action_payload or {})

            if c_step.action_strategy == ActionStrategy.SEND_HOTKEY:
                try:
                    from app.services.missions.vibe_parser import validate_hotkey_payload
                    payload = validate_hotkey_payload(payload)
                except Exception:
                    pass

            elif c_step.action_strategy == ActionStrategy.TYPE_TEXT:
                if "text" not in payload:
                    payload["text"] = str(payload.get("text", ""))

            elif c_step.action_strategy == ActionStrategy.SET_FIELD_VALUE:
                payload["text"] = str(payload.get("text", ""))
                payload.setdefault("label", "")
                payload.setdefault("control_type_hint", "")
                payload.setdefault("web_role_hint", "")
                payload.setdefault("web_tag_hint", "")
                payload.setdefault("fusion_confidence", "weak")

            elif c_step.action_strategy == ActionStrategy.SCROLL:
                payload.setdefault("dx", 0)
                payload.setdefault("dy", 0)
                payload["dx"] = int(payload.get("dx") or 0)
                payload["dy"] = int(payload.get("dy") or 0)

            elif c_step.action_strategy == ActionStrategy.DRAG_DROP:
                for k in ("start_x", "start_y", "end_x", "end_y"):
                    payload[k] = int(payload.get(k) or 0)
                payload["duration"] = float(payload.get("duration") or 0.4)

            elif c_step.action_strategy in (
                ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK, ActionStrategy.RIGHT_CLICK
            ):
                # Para clicks, aseguramos coords de fallback cuando existan.
                if not c_step.target_context.fallback_coords and payload.get("x") and payload.get("y"):
                    c_step.target_context.fallback_coords = {
                        "x": int(payload["x"]), "y": int(payload["y"])
                    }

            c_step.action_payload = payload

            # Descripción nunca vacía
            if not (i_step.description or "").strip():
                i_step.description = f"{c_step.action_strategy.value.replace('_', ' ')}"

            # ── Validación mínima por acción (PRD §6) ─────────────
            # Ningún step puede salir con validation_strategy=NONE: el
            # ApprovalGate refuse a aprobar y debemos evitar el rebote.
            if c_step.validation_strategy == ValidationStrategy.NONE:
                c_step.validation_strategy = _default_validation_for(
                    c_step.action_strategy,
                )

        except Exception as e:
            # No bloqueamos la compilación por una normalización puntual
            try:
                from app.core.logger import log as _log
                _log.debug(f"_normalize_step: {e}")
            except Exception:
                pass
