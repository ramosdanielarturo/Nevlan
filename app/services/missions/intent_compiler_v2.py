"""
Nevlan — Intent Compiler V2
============================

Compilador semántico de **intención**, no de eventos.

El compiler clásico (``compiler.py``) traduce ``raw_trace → CompiledStep``
agrupando eventos por proximidad temporal. Funciona para macros simples
pero produce ruido cuando la grabación tiene clicks débiles, texto
fragmentado y delays humanos.

Intent Compiler V2 toma una óptica distinta:

  1. **Sanitiza** el raw_trace (raw_event_sanitizer).
  2. **Identifica sesiones de campo** (field_session_manager_v2):
     "el usuario tipó X en Y y opcionalmente confirmó".
  3. **Detecta contextos** (Chrome abierto, profile picker, YouTube
     cargado…) usando WindowFingerprint + heurísticas.
  4. **Promueve** lo anterior a UN PUÑADO de bloques semánticos:
        Bloque 1: Abrir Chrome
            * open_app chrome
            * select_profile <name>
        Bloque 2: Abrir YouTube
            * open_new_tab
            * open_url youtube.com
        Bloque 3: Buscar
            * search_youtube query="..."
        Bloque 4 (opcional): scroll_results
  5. **Inyecta waits** entre bloques con ``wait_manager.suggest_wait_for``.
  6. **Rescata** clicks con target inválido (invalid_target_resolver).

La salida es **una lista de IntentBlock** (humana, simple) y, encima,
una lista plana de ``CompiledStep`` lista para el Smart Executor.

Reglas
------

* Fusiona pasos.
* Elimina clicks intermedios (los que el sanitizer ya borró).
* Elimina enter separado (la sesión de campo lo absorbe).
* Elimina texto separado (la sesión consolida).

API
---

>>> blocks, steps = compile_intent(mission)   # solo computa, no muta
>>> apply_intent_to_mission(mission)          # muta in-place y devuelve report

``apply_intent_to_mission`` REEMPLAZA ``compiled_execution_graph`` y
``interpreted_steps`` con la versión V2. El compiler clásico sigue
disponible — el caller decide qué versión usar (V2 es opt-in).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.contracts.mission import (
    ActionStrategy,
    CompiledStep,
    EventType,
    InterpretedStep,
    Mission,
    RawEvent,
    TargetContext,
    ValidationStrategy,
)
from app.core.logger import log
from app.services.missions.field_session_manager import (
    FieldSession,
    build_field_sessions,
    event_ids_consumed,
)
from app.services.missions.invalid_target_resolver import (
    rescue_steps,
    youtube_search_locators,
)
from app.services.missions.raw_event_sanitizer import sanitize_raw_trace


# ──────────────────────────────────────────────────────────────────────
# Modelo de bloques semánticos
# ──────────────────────────────────────────────────────────────────────

@dataclass
class IntentStep:
    """Un step semántico compacto (la cara humana de un CompiledStep).

    Coincide en forma con la salida que el ``semantic_adapter`` espera
    como entrada: ``{type, params, _source_step_id}``.
    """
    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    human_label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "params": dict(self.params),
            "human_label": self.human_label,
        }


@dataclass
class IntentBlock:
    """Bloque semántico humano.  Ej. "Abrir Chrome", "Buscar canción"."""
    title: str
    steps: List[IntentStep] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class CompileReport:
    """Reporte de la compilación V2 — útil para telemetría y UI."""
    sanitized_input: int = 0
    sanitized_output: int = 0
    sessions_detected: int = 0
    blocks_emitted: int = 0
    steps_emitted: int = 0
    rescued_invalid_targets: int = 0
    waits_injected: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "input_events": self.sanitized_input,
            "clean_events": self.sanitized_output,
            "sessions": self.sessions_detected,
            "blocks": self.blocks_emitted,
            "steps": self.steps_emitted,
            "rescued": self.rescued_invalid_targets,
            "waits": self.waits_injected,
        }


# ──────────────────────────────────────────────────────────────────────
# Detección de contextos
# ──────────────────────────────────────────────────────────────────────

@dataclass
class _Context:
    """Estado lógico que vamos componiendo a partir del trace."""
    chrome_opened: bool = False
    selected_profile: Optional[str] = None
    last_url: Optional[str] = None
    new_tab_seen: bool = False
    sites_visited: List[str] = field(default_factory=list)


def _detect_chrome_open(events: List[RawEvent]) -> bool:
    """¿Hubo en algún momento un WINDOW_ACTIVATE de Chrome/Edge/Brave?"""
    for ev in events:
        if ev.event_type != EventType.WINDOW_ACTIVATE:
            continue
        proc = ""
        if ev.window_context:
            proc = (ev.window_context.process_name or "").lower()
        if any(p in proc for p in ("chrome", "msedge", "brave")):
            return True
    return False


def _detect_profile_picker(events: List[RawEvent]) -> Optional[str]:
    """¿Hay un click en la pantalla 'Quién eres?'? Devuelve el nombre
    del perfil si lo recuperó por OCR cercano, o '' si lo detectó pero
    sin nombre fiable, o None si no hay picker.
    """
    for ev in events:
        if ev.event_type not in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DOUBLE_CLICK,
        ):
            continue
        meta = ev.metadata or {}
        win = ev.window_context
        title = (win.title if win else "").lower()
        proc = (win.process_name if win else "").lower()
        if not any(p in proc for p in ("chrome", "msedge", "brave")):
            continue
        if any(
            kw in title for kw in (
                "quién eres", "who are you", "elige tu perfil",
                "choose a profile", "select profile",
            )
        ):
            vis = meta.get("vision_data") or {}
            ocr = ""
            if isinstance(vis, dict):
                ocr = str(vis.get("ocr_text_around") or "").strip()
            cand = []
            for tok in ocr.split():
                if tok and tok[0].isupper():
                    cand.append(tok)
                if len(cand) >= 3:
                    break
            return " ".join(cand).strip(",.;:") if cand else ""
    return None


def _detect_new_tab(events: List[RawEvent]) -> bool:
    """¿Se hizo Ctrl+T o se clickeó el botón '+' de Chrome?"""
    for ev in events:
        if ev.event_type == EventType.KEYBOARD_HOTKEY and ev.keyboard_action:
            keys = [str(k).lower() for k in ev.keyboard_action.keys]
            mods = [str(m).lower() for m in ev.keyboard_action.modifiers]
            if "t" in keys and ("ctrl" in mods or "control" in mods):
                return True
        if ev.event_type in (
            EventType.MOUSE_CLICK, EventType.MOUSE_DOUBLE_CLICK
        ):
            uia = (ev.metadata or {}).get("uia_data") or {}
            name = str(uia.get("name") or "").lower() if isinstance(uia, dict) else ""
            if "nueva pestaña" in name or "new tab" in name:
                return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Promotion: sesiones → IntentSteps
# ──────────────────────────────────────────────────────────────────────

def _looks_like_url(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.startswith(("http://", "https://", "www.")):
        return True
    if "." in t and " " not in t:
        tld = t.rsplit(".", 1)[-1]
        return 2 <= len(tld) <= 6 and tld.isalpha()
    return False


def _site_to_open_url(site: str, query: str) -> Optional[str]:
    """Si la sesión es 'búsqueda en youtube' pero el usuario aún NO
    estaba en youtube.com, primero hay que abrir youtube.com.
    """
    if site == "youtube":
        return "youtube.com"
    if site == "google":
        return "google.com"
    return None


def _session_to_intent_steps(s: FieldSession) -> List[IntentStep]:
    """Convierte una sesión en uno o varios IntentSteps semánticos."""
    text = s.final_text.strip()
    if not text:
        return []
    out: List[IntentStep] = []

    # Caso 1: Windows Search (proceso explorer + título Search) →
    #         launch_app por nombre.
    if s.field_label == "windows_search" and s.submitted:
        out.append(IntentStep(
            type="open_app",
            params={"app": text, "query": text},
            human_label=f"Abrir {text}",
        ))
        return out

    # Caso 2: Browser address bar → open_url o search_youtube si query.
    if s.field_label == "browser_address_bar" and s.submitted:
        if _looks_like_url(text):
            out.append(IntentStep(
                type="open_url",
                params={"url": text},
                human_label=f"Ir a {text}",
            ))
        else:
            # Texto en address bar = búsqueda con buscador por defecto.
            out.append(IntentStep(
                type="search",
                params={"val": text, "submit": True},
                human_label=f"Buscar: {text}",
            ))
        return out

    # Caso 3: YouTube search box.
    if s.site == "youtube":
        out.append(IntentStep(
            type="search_youtube",
            params={
                "query": text,
                "site": "youtube",
                "target": "youtube_search_box",
                "locators": youtube_search_locators(),
            },
            human_label=f"Buscar en YouTube: {text}",
        ))
        return out

    # Caso 4: campo genérico — fill_field (no es ejecutable por
    # smart-executor pero sí por el player legacy).
    out.append(IntentStep(
        type="fill_field" if not s.submitted else "search",
        params={
            "val": text,
            "target": s.field_label or "input",
            "submit": s.submitted,
        },
        human_label=f"Escribir en {s.field_label or 'campo'}: {text}",
    ))
    return out


# ──────────────────────────────────────────────────────────────────────
# IntentSteps → CompiledSteps
# ──────────────────────────────────────────────────────────────────────

_TYPE_TO_ACTION = {
    "open_app": ActionStrategy.LAUNCH_APP,
    "select_profile": ActionStrategy.SELECT_PROFILE,
    "open_new_tab": ActionStrategy.OPEN_NEW_TAB,
    "open_url": ActionStrategy.OPEN_BOOKMARK,  # se maneja como open_url
    "search_youtube": ActionStrategy.SUBMIT_SEARCH,
    "search": ActionStrategy.SUBMIT_SEARCH,
    "fill_field": ActionStrategy.SET_FIELD_VALUE,
    "scroll_results": ActionStrategy.SCROLL_UNTIL_VISIBLE,
    "wait_for_state": ActionStrategy.WAIT_FOR_STATE,
}


def _intent_to_compiled(step: IntentStep) -> CompiledStep:
    a = _TYPE_TO_ACTION.get(step.type, ActionStrategy.AGENT_PROMPT)
    payload: Dict[str, Any] = dict(step.params)
    semantic: Dict[str, Any] = {
        "intent": step.type,
        "human_label": step.human_label,
    }
    # Mapeos específicos:
    if step.type == "open_app":
        payload = {
            "app_name": step.params.get("app") or step.params.get("name"),
            "query": step.params.get("query"),
        }
    if step.type == "search_youtube":
        payload = {
            "site": "youtube",
            "text": step.params.get("query") or step.params.get("val") or "",
            "submit_after": True,
            "target_label": step.params.get("target") or "youtube_search_box",
            "locators": step.params.get("locators") or youtube_search_locators(),
        }
        semantic.update({
            "site": "youtube",
            "expected_state_after": "youtube_results_visible",
        })
    if step.type == "search":
        payload = {
            "text": step.params.get("val") or step.params.get("query") or "",
            "submit_after": bool(step.params.get("submit")),
        }
        if step.params.get("site"):
            payload["site"] = step.params["site"]
        if step.params.get("target"):
            payload["target_label"] = step.params["target"]
    if step.type == "open_url":
        payload = {
            "name": step.params.get("name") or "",
            "url": step.params.get("url") or "",
        }
    if step.type == "fill_field":
        payload = {
            "text": step.params.get("val") or "",
            "target_label": step.params.get("target") or "",
            "submit_after": False,
            "field_session": True,
        }
    if step.type == "wait_for_state":
        payload = dict(step.params)

    cs = CompiledStep(
        goal=step.human_label or step.type,
        action_strategy=a,
        action_payload=payload,
        target_context=TargetContext(semantic=semantic),
        validation_strategy=ValidationStrategy.REQUIRE_ELEMENT_EXISTS,
    )
    return cs


# ──────────────────────────────────────────────────────────────────────
# Bloque builder
# ──────────────────────────────────────────────────────────────────────

def _build_blocks(
    sessions: List[FieldSession],
    chrome_opened: bool,
    profile_name: Optional[str],
    new_tab: bool,
) -> List[IntentBlock]:
    blocks: List[IntentBlock] = []

    # Bloque 1: Abrir Chrome (si aplica).
    if chrome_opened:
        b1 = IntentBlock(title="Abrir Chrome", summary="")
        b1.steps.append(IntentStep(
            type="open_app",
            params={"app": "chrome"},
            human_label="Abrir Chrome",
        ))
        if profile_name is not None:
            b1.steps.append(IntentStep(
                type="select_profile",
                params={
                    "profile": profile_name or "",
                    "needs_user_label": not bool(profile_name),
                    "label_prompt": "¿Qué perfil seleccionaste?" if not profile_name else "",
                },
                human_label=f"Seleccionar perfil {profile_name or '(?)'}",
            ))
        blocks.append(b1)

    # Bloque 2: Abrir destino.
    # Si la primera sesión "submitted" no es Windows Search, sí es un
    # search_youtube/search/open_url, asumimos "abrir destino" antes de
    # la sesión.
    target_sessions = [s for s in sessions if s.submitted and s.field_label != "windows_search"]
    address_bar = next((s for s in sessions if s.field_label == "browser_address_bar"), None)
    youtube_session = next((s for s in sessions if s.site == "youtube" and s.submitted), None)

    if address_bar and address_bar.submitted:
        # El usuario navegó manualmente (Ctrl+L + URL).
        url = address_bar.final_text.strip()
        b2 = IntentBlock(title="Abrir destino", summary="")
        if new_tab:
            b2.steps.append(IntentStep(
                type="open_new_tab",
                params={"app": "chrome"},
                human_label="Abrir nueva pestaña",
            ))
        if _looks_like_url(url):
            b2.steps.append(IntentStep(
                type="open_url",
                params={"url": url},
                human_label=f"Ir a {url}",
            ))
        blocks.append(b2)
    elif youtube_session:
        b2 = IntentBlock(title="Abrir YouTube", summary="")
        if new_tab:
            b2.steps.append(IntentStep(
                type="open_new_tab",
                params={"app": "chrome"},
                human_label="Abrir nueva pestaña",
            ))
        b2.steps.append(IntentStep(
            type="open_url",
            params={"url": "youtube.com"},
            human_label="Ir a YouTube",
        ))
        blocks.append(b2)

    # Bloque 3: Buscar / acción principal (cada sesión submitted).
    for s in sessions:
        steps = _session_to_intent_steps(s)
        if not steps:
            continue
        title = "Buscar" if s.submitted else "Llenar campo"
        if s.site == "youtube":
            title = "Buscar en YouTube"
        if s.field_label == "windows_search":
            title = "Abrir aplicación"
        # Si el step ya quedó en bloque previo (address_bar →
        # open_url), no lo dupliques.
        if s is address_bar:
            continue
        if s is youtube_session:
            # Search step va en bloque "Buscar".
            blk = IntentBlock(title=title, summary="")
            blk.steps.extend(steps)
            blocks.append(blk)
            continue
        blk = IntentBlock(title=title, summary="")
        blk.steps.extend(steps)
        blocks.append(blk)

    return blocks


# ──────────────────────────────────────────────────────────────────────
# Inyección de waits entre bloques
# ──────────────────────────────────────────────────────────────────────

def _inject_waits(blocks: List[IntentBlock], rep: CompileReport) -> List[IntentBlock]:
    try:
        from app.services.missions.wait_manager import suggest_wait_for
    except Exception:
        return blocks

    out: List[IntentBlock] = []
    last_step: Optional[IntentStep] = None
    for blk in blocks:
        new_steps: List[IntentStep] = []
        for step in blk.steps:
            if last_step is not None:
                w = suggest_wait_for(last_step.type, last_step.params)
                if w:
                    new_steps.append(IntentStep(
                        type=w["kind"],
                        params=w["params"],
                        human_label=f"Esperar a {w['params'].get('condition','')}",
                    ))
                    rep.waits_injected += 1
            new_steps.append(step)
            last_step = step
        out.append(IntentBlock(
            title=blk.title, summary=blk.summary, steps=new_steps,
        ))
    return out


# ──────────────────────────────────────────────────────────────────────
# API pública
# ──────────────────────────────────────────────────────────────────────

def compile_intent(
    mission: Mission,
) -> Tuple[List[IntentBlock], List[CompiledStep], CompileReport]:
    """Toma la misión cruda (raw_trace) y produce bloques + steps V2.

    NO modifica la misión. Para mutarla, usar
    ``apply_intent_to_mission``.

    Implementación V2-final
    -----------------------
    Desde el sprint del Intent Collapse Engine, la lógica primaria de
    detección + colapso se delega a
    :func:`intent_collapse_engine.collapse_intent`. Aquí solo:

      1. Sanitizamos el trace (telemetría — ya idempotente).
      2. Construimos field_sessions (telemetría).
      3. Llamamos al Collapse Engine.
      4. Convertimos su salida a ``IntentBlock`` + ``CompiledStep`` para
         mantener compat con el resto del pipeline (Mission Review
         clásico, semantic_adapter, etc.).
    """
    rep = CompileReport()
    if not mission or not mission.raw_trace:
        return [], [], rep

    # Imports locales para evitar ciclos.
    from app.services.missions.intent_collapse_engine import (
        collapse_intent as _collapse_intent,
        plan_to_compiled_steps as _plan_to_compiled,
    )

    # Step 1: sanitizar (para telemetría; el engine sanitiza también).
    cleaned, srep = sanitize_raw_trace(list(mission.raw_trace))
    rep.sanitized_input = srep.input_count
    rep.sanitized_output = srep.output_count

    # Step 2: sesiones (telemetría).
    sessions = build_field_sessions(cleaned)
    rep.sessions_detected = len(sessions)

    # Step 3: COLAPSO semántico — produce el plan canónico.
    plan = _collapse_intent(
        raw_trace=cleaned,
        field_sessions=sessions,
        compiled_execution_graph=list(mission.compiled_execution_graph or []),
        interpreted_steps=list(mission.interpreted_steps or []),
    )

    # Step 4: convertir el plan a IntentBlock (compat) + CompiledStep.
    blocks: List[IntentBlock] = []
    for blk in plan.blocks:
        ib = IntentBlock(title=blk.name, summary=blk.name)
        for s in blk.steps:
            ib.steps.append(IntentStep(
                type=s.type,
                params=dict(s.params),
                human_label=s.human_label or "",
            ))
        blocks.append(ib)
    compiled: List[CompiledStep] = _plan_to_compiled(plan)

    rep.blocks_emitted = len(blocks)
    rep.steps_emitted = len(compiled)
    rep.rescued_invalid_targets = 0

    log.info(f"[intent_compiler_v2] {rep.as_dict()}")
    return blocks, compiled, rep


def apply_intent_to_mission(mission: Mission) -> CompileReport:
    """Sobrescribe ``compiled_execution_graph``, ``interpreted_steps`` y
    ``action_groups`` con la salida del Intent Collapse Engine.

    Es destructivo: si el caller quería conservar el grafo legacy,
    debe clonar la misión antes.
    """
    from app.services.missions.intent_collapse_engine import (
        apply_collapse_to_mission,
    )

    rep = CompileReport()
    if not mission or not mission.raw_trace:
        return rep

    # Telemetría (input/output).
    cleaned, srep = sanitize_raw_trace(list(mission.raw_trace))
    rep.sanitized_input = srep.input_count
    rep.sanitized_output = srep.output_count
    sessions = build_field_sessions(cleaned)
    rep.sessions_detected = len(sessions)

    # El collapse engine es la fuente de verdad: muta la misión y
    # devuelve el plan. ``apply_intent_to_mission`` es semánticamente
    # destructivo (su contrato dice "sobrescribe…"), así que pedimos
    # rewrite explícito.
    plan = apply_collapse_to_mission(mission, rewrite_compiled_graph=True)

    rep.blocks_emitted = len(plan.blocks)
    rep.steps_emitted = plan.step_count
    rep.rescued_invalid_targets = 0
    log.info(f"[intent_compiler_v2/apply] {rep.as_dict()}")
    return rep


__all__ = [
    "IntentStep",
    "IntentBlock",
    "CompileReport",
    "compile_intent",
    "apply_intent_to_mission",
]
