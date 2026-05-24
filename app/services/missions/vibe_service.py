"""
Nevlan — Vibe Service (unificado)
---------------------------------
Una sola fuente de verdad para el "Vibe Edit" de un paso compilado.

Uso típico:

    from app.services.missions.vibe_service import apply_vibe_edit
    result = apply_vibe_edit(mission, index, user_instruction,
                             on_confirm_agent=lambda: True)
    if result["ok"]:
        mission_store.save(mission)
        refresh_ui()

Reglas del servicio:
- Fast path local primero (teclado, tipeo) — rápido y sin gasto de LLM.
- Si el fast path no aplica, cae al LLM (agent.process_user_input).
- Nunca convierte pasos deterministas (click/type/hotkey) a agent_prompt
  sin `on_confirm_agent()` devolviendo True.
- Valida y normaliza siempre el payload final.
- No abre diálogos ni depende de PyQt: es 100% lógico.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Dict, Optional

from app.contracts.mission import Mission, ActionStrategy
from app.core.logger import log
from app.services.missions.vibe_parser import (
    parse_keyboard_intent, parse_type_text_intent,
    validate_hotkey_payload, describe_hotkey_payload,
)


DETERMINISTIC_STRATEGIES = {
    ActionStrategy.CLICK, ActionStrategy.DOUBLE_CLICK, ActionStrategy.RIGHT_CLICK,
    ActionStrategy.TYPE_TEXT, ActionStrategy.SEND_HOTKEY,
    ActionStrategy.SCROLL, ActionStrategy.DRAG_DROP, ActionStrategy.WAIT_FOR_STATE,
}


def apply_vibe_edit(
    mission: Mission,
    index: int,
    instruction: str,
    on_confirm_agent: Optional[Callable[[], bool]] = None,
) -> Dict:
    """Aplica un Vibe Edit al paso `index`. Devuelve un dict con el resultado:

    {
      "ok": bool,
      "path": "fast_kb" | "fast_type" | "llm" | "noop",
      "description": str,
      "error": str,
    }
    """
    result = {"ok": False, "path": "noop", "description": "", "error": ""}

    if not mission or not instruction or not instruction.strip():
        result["error"] = "Instrucción vacía"
        return result
    if index < 0 or index >= len(mission.compiled_execution_graph):
        result["error"] = "Índice fuera de rango"
        return result

    c_step = mission.compiled_execution_graph[index]
    i_step = (
        mission.interpreted_steps[index]
        if index < len(mission.interpreted_steps) else None
    )
    current_action = getattr(c_step.action_strategy, "value", str(c_step.action_strategy))
    is_deterministic = c_step.action_strategy in DETERMINISTIC_STRATEGIES

    text = instruction.strip()

    # ────────────────────────── FAST PATH: KEYBOARD ──────────────────────────
    kb_payload = parse_keyboard_intent(text)
    if kb_payload:
        try:
            c_step.action_strategy = ActionStrategy.SEND_HOTKEY
            c_step.action_payload = validate_hotkey_payload(kb_payload)
            desc = describe_hotkey_payload(c_step.action_payload)
            if i_step is not None:
                i_step.description = desc
            result.update(ok=True, path="fast_kb", description=desc)
            log.info(f"vibe_service.fast_kb: paso {index} → {desc}")
            return result
        except Exception as e:
            log.error(f"vibe_service.fast_kb failed: {e}")

    # ────────────────────────── FAST PATH: TYPE TEXT ─────────────────────────
    type_payload = parse_type_text_intent(text)
    if type_payload:
        try:
            c_step.action_strategy = ActionStrategy.TYPE_TEXT
            c_step.action_payload = {"text": type_payload["text"]}
            show = type_payload["text"]
            show = show if len(show) <= 40 else show[:40] + "…"
            desc = f"⌨ Escribir '{show}'"
            if i_step is not None:
                i_step.description = desc
            result.update(ok=True, path="fast_type", description=desc)
            log.info(f"vibe_service.fast_type: paso {index}")
            return result
        except Exception as e:
            log.error(f"vibe_service.fast_type failed: {e}")

    # ────────────────────────── LLM FALLBACK ────────────────────────────────
    _key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPEN_AI_API_KEY")
        or ""
    ).strip()
    if not _key:
        result["error"] = (
            "Las sugerencias con IA requieren OPENAI_API_KEY en el entorno "
            "(archivo `.env` o variables de sistema; véase `.env.example`). "
            "Los atajos de teclado y «escribir texto» sin IA siguen disponibles."
        )
        return result

    try:
        sys_prompt = _build_llm_prompt(current_action, i_step, c_step, text)
        from app.brain.agent import agent
        resp = agent.process_user_input(sys_prompt) or ""
        data = _safe_parse_json(resp)
        if not data:
            result["error"] = "LLM no devolvió JSON válido"
            return result

        intent = data.get("intent", "description_change")
        new_action = data.get("action", current_action)

        # Protección: no pases un paso determinista a agent_prompt sin confirmar.
        if new_action == "agent_prompt" and is_deterministic:
            if not on_confirm_agent or not on_confirm_agent():
                result["error"] = "Conversión a agent_prompt cancelada"
                return result

        if intent == "description_change":
            if "description" in data and i_step is not None:
                i_step.description = data["description"]
        elif intent == "payload_change":
            if isinstance(data.get("payload"), dict):
                c_step.action_payload.update(data["payload"])
            if "description" in data and i_step is not None:
                i_step.description = data["description"]
        elif intent in ("target_change", "strategy_change", "agentic_change"):
            if new_action and new_action != current_action:
                try:
                    c_step.action_strategy = ActionStrategy(new_action)
                except Exception:
                    pass
            if isinstance(data.get("payload"), dict):
                c_step.action_payload = dict(data["payload"])
            if "description" in data and i_step is not None:
                i_step.description = data["description"]

        # Normalización final
        if c_step.action_strategy == ActionStrategy.SEND_HOTKEY:
            c_step.action_payload = validate_hotkey_payload(c_step.action_payload)
            if i_step is not None:
                auto_desc = describe_hotkey_payload(c_step.action_payload)
                if not (i_step.description or "").strip() or \
                        (i_step.description or "").lower().startswith("paso"):
                    i_step.description = auto_desc
        elif c_step.action_strategy == ActionStrategy.TYPE_TEXT:
            if "text" not in c_step.action_payload:
                c_step.action_payload["text"] = str(c_step.action_payload.get("text", ""))

        final_desc = i_step.description if i_step is not None else ""
        result.update(ok=True, path="llm", description=final_desc)
        log.info(f"vibe_service.llm: paso {index} → {final_desc}")
    except Exception as e:
        log.error(f"vibe_service.llm failed: {e}")
        result["error"] = str(e)

    return result


# ────────────────────────────────────────────────────────────────────────────
# Internals
# ────────────────────────────────────────────────────────────────────────────

def _build_llm_prompt(current_action: str, i_step, c_step, user_text: str) -> str:
    desc = i_step.description if i_step is not None else ""
    payload_json = json.dumps(c_step.action_payload, ensure_ascii=False)
    return (
        f"Eres el clasificador de Vibe Coding de Nevlan. "
        f"El paso actual es: action='{current_action}', description='{desc}', "
        f"payload={payload_json}.\n"
        f"El usuario pide: '{user_text}'.\n\n"
        f"Clasifica la intención y responde SOLO JSON:\n"
        f"{{\n"
        f'  "intent": "description_change|payload_change|target_change|strategy_change|agentic_change",\n'
        f'  "action": "click|double_click|right_click|type_text|send_hotkey|agent_prompt|scroll|drag_drop|wait_for_state",\n'
        f'  "payload": {{}},\n'
        f'  "description": "nueva descripción corta (max 8 palabras)"\n'
        f"}}\n\n"
        f"PAYLOADS VÁLIDOS:\n"
        f'- type_text:   {{"text": "texto a escribir"}}\n'
        f'- send_hotkey COMBO (Ctrl+S, Alt+F4): {{"combo": true, "keys": ["ctrl","s"]}}\n'
        f'- send_hotkey SECUENCIA (tab, enter): {{"keys": ["tab","enter"]}}\n'
        f'- send_hotkey REPEAT: {{"key": "tab", "repeat": 3}}\n'
        f'- send_hotkey ÚNICA: {{"hotkey": "enter"}}\n'
        f'- wait_for_state: {{"timeout_ms": 500}}\n'
        f'- scroll: {{"dy": -3}}\n'
        f'- agent_prompt: {{"text": "instrucción en lenguaje natural"}}\n\n'
        f"REGLAS:\n"
        f"- 'tab tres veces' → key+repeat.\n"
        f"- 'tab, tab, enter' → keys=[...] (sin combo).\n"
        f"- 'ctrl+s' → combo=true.\n"
        f"- NUNCA mezcles keys+repeat+combo.\n"
        f"- NUNCA conviertas un click/type/hotkey a agent_prompt si no es realmente necesario.\n"
    )


def _safe_parse_json(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    content = raw.strip()
    if content.startswith("```json"):
        content = content.split("```json", 1)[-1]
    elif content.startswith("```"):
        content = content.split("```", 1)[-1]
    if content.endswith("```"):
        content = content.rsplit("```", 1)[0]
    content = content.strip()
    try:
        return json.loads(content)
    except Exception:
        return None
