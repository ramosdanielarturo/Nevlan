"""
ArthurOS Brain - Agent (Smart Capabilities)
-------------------------------------------
System Prompt actualizado para usar Playwright y Visión.
"""
import json
import threading
import re
from app.core.logger import log
from app.contracts.tool_call import ToolCall
from app.contracts.tool_result import ToolResult
from app.security.policy import policy, Decision
from app.services.llm.factory import get_llm_provider
from app.skills.registry import registry
from app.services.missions.recorder import get_recorder
from app.contracts.mission import AnnotationType

# Importar TODAS las tools
import app.skills.tools.os
import app.skills.tools.fs.read
import app.skills.tools.fs.write
import app.skills.tools.desktop
import app.skills.tools.desktop_automation
import app.skills.tools.web
import app.skills.tools.terminal

# Misiones: lazy import (no falla si hay problema)
try:
    import app.skills.tools.missions
except Exception as e:
    log.warning(f"Misiones no disponibles: {e}")

try:
    from app.runtime.bus import bus
    from app.contracts.events import SystemEvent
except Exception:
    bus = None

class Agent:
    def __init__(self, max_tool_loops: int = 10):
        self.llm = get_llm_provider()
        self.max_tool_loops = max_tool_loops
        self.max_history_messages = 20
        
        # Cancellation mechanism (thread-safe)
        self._cancelled_lock = threading.Lock()
        self._cancelled = False
        
        self.system_prompt = (
            "Eres ArthurOS, un OPERADOR INTELIGENTE y ROBUSTO de Windows. No eres un simple chatbot.\n"
            "Tu misión es ejecutar tareas complejas en el ordenador del usuario con precisión quirúrgica.\n\n"
            "REGLAS OPERATIVAS (MANDAMIENTOS):\n"
            "1. PENSAMIENTO ESTRUCTURADO:\n"
            "   - ANTES de actuar, PIENSA. Define: Objetivo -> Plan -> Ejecución -> Verificación.\n"
            "   - Si la tarea es compleja, divídela en pasos pequeños.\n\n"
            "2. MODO ANFIBIO (Context Awareness):\n"
            "   - PRIMERO: Detecta dónde estás ('desktop.scan_context' o 'web.scan_context').\n"
            "   - WEB: Usa selectores semánticos ('text=Login'). NO coordenadas.\n"
            "   - DESKTOP: Usa 'desktop.uia.click' (Nombre/ID). NO coordenadas ciegas.\n"
            "   - ANTIRUIDO: Si el usuario dice 'sí', 'ok', 'gracias' o algo menor a 3 palabras sin contexto claro, ESCUCHÓ RUIDO AMBIENTAL. NO EJECUTES NINGUNA HERRAMIENTA. Solo asiente.\n\n"
            "3. MISIONES (Super RPA / Playbooks):\n"
            "   - 'missions.search(query)': BUSCA PRIMERO si ya existe una misión para lo que quieres hacer.\n"
            "   - 'missions.run(name, variables={...})': Ejecuta la misión. Usa variables si es necesario (ej: {'user': 'dani'}).\n"
            "   - 'missions.start_recording' / 'stop_recording': Graba nuevas misiones si no existen.\n"
            "   - 'missions.add_annotation('variable', 'nombre')': Marca texto variable durante la grabación.\n"
            "   - IMPORTANTE: NUNCA inicies y detengas una grabación en la misma respuesta. Si el usuario pide iniciar, SOLO usa 'start_recording' y TERMINA TU TURNO, dejándolo trabajar el tiempo que necesite.\n"
            "   - REGLA GENERAL DE TURNO: Si iniciaste, detuviste, guardaste o EJECUTASTE una misión, TERMINA TU TURNO inmediatamente (no llames más tools). Informa al usuario y detente.\n\n"
            "4. ROBUSTEZ Y AUTOCORRECCIÓN:\n"
            "   - ESTRATEGIA DE FALLO: Intentar UIA -> Fallar a OCR ('vision.click_text') -> Fallar a Template ('vision.click_image').\n"
            "   - SIEMPRE enfoca la ventana ('desktop.window.focus') antes de interactuar.\n"
            "   - Si una acción falla, lee el error, ajusta tu plan y reintenta. No repitas el mismo error.\n"
            "   - SI NOTAS FALLOS REPETIDOS: Usa 'telemetry.get_health()' para ver si una herramienta está rota.\n\n"
            "4. VERIFICACIÓN:\n"
            "   - Después de cada paso crítico, verifica que ocurrió (ej: ¿Se abrió Notepad? ¿Cambió la URL?).\n"
            "   - Si no estás seguro, usa 'desktop.scan_context' para ver el estado actual.\n\n"
            "5. HERRAMIENTAS:\n"
            "   - 'run_command': Para lanzar apps (ej: 'scalc', 'notepad', 'chrome').\n"
            "   - 'desktop.uia.*': Para interactuar fiable con apps nativas.\n"
            "   - 'vision.*': Para leer pantalla (OCR) o buscar iconos.\n\n"
            "Compórtate como un ingeniero experto. Sé conciso en el chat, pero prolífico en la acción."
        )
        self.history = [{"role": "system", "content": self.system_prompt}]

    def _emit(self, name: str, payload: dict):
        if bus: bus.publish(SystemEvent(name=name, payload=payload))

    def _trim_history(self):
        if len(self.history) <= self.max_history_messages: return
        sys_msg = self.history[0]
        candidates = self.history[-(self.max_history_messages):]
        while candidates and candidates[0].get("role") == "tool":
            candidates.pop(0)
        self.history = [sys_msg] + candidates

    def cancel_current_request(self):
        """Cancel the current processing request (thread-safe)."""
        with self._cancelled_lock:
            self._cancelled = True
            log.warning("Agent cancellation requested")
    
    def _check_cancelled(self):
        """Check if current request has been cancelled (thread-safe)."""
        with self._cancelled_lock:
            return self._cancelled
    
    def _cleanup_incomplete_tool_calls(self):
        """
        Ensure all tool_calls in history have corresponding tool responses.
        This prevents OpenAI API 400 errors when operations are interrupted.
        """
        # Find the last assistant message with tool_calls
        for i in range(len(self.history) - 1, -1, -1):
            msg = self.history[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # Check if all tool_calls have responses
                tool_call_ids = {tc["id"] for tc in msg["tool_calls"]}
                
                # Find existing tool responses after this message
                responded_ids = set()
                for j in range(i + 1, len(self.history)):
                    if self.history[j].get("role") == "tool":
                        responded_ids.add(self.history[j]["tool_call_id"])
                
                # Add error responses for missing tool_calls
                missing_ids = tool_call_ids - responded_ids
                for tc_id in missing_ids:
                    log.warning(f"Adding error response for orphaned tool_call: {tc_id}")
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": "ERROR: Operation cancelled by user"
                    })
                break  # Only check the most recent assistant message
    
    def _validate_history(self):
        """
        Validate that history is in a valid state for OpenAI.
        Fixes any incomplete tool_calls before sending to LLM.
        """
        self._cleanup_incomplete_tool_calls()

    def process_user_input(self, user_text: str) -> str:
        # Reset cancellation flag at start of new request
        with self._cancelled_lock:
            self._cancelled = False
            
        # 1. INTERCEPTOR DE ANOTACIONES (Fast-Track)
        recorder = get_recorder()
        if recorder and recorder.is_recording():
            # Regex para detectar comandos directos de voz O texto (sin requerir dos puntos)
            match = re.match(r'^\s*(anotación|anotacion|nota|etiqueta|variable|verificación|verificacion|fallback)\b[:\-]?\s+(.+)$', user_text.strip(), re.IGNORECASE)
            
            if match:
                keyword = match.group(1).lower()
                content = match.group(2).strip()
                
                # Clasificación inteligente
                ann_type = AnnotationType.NOTE
                if keyword == "etiqueta": ann_type = AnnotationType.LABEL
                elif keyword == "variable": ann_type = AnnotationType.VARIABLE
                elif keyword in ["verificación", "verificacion"]: ann_type = AnnotationType.VERIFICATION
                elif keyword == "fallback": ann_type = AnnotationType.FALLBACK
                elif keyword in ["anotación", "anotacion", "nota"]:
                    # Heurística para detectar fallbacks encubiertos
                    if any(word in content.lower() for word in ["si falla", "si no", "cambia", "busca", "error"]):
                        ann_type = AnnotationType.FALLBACK

                # Guardar la anotación con pegado automático
                recorder.add_annotation(ann_type.value, content)
                
                # Retorno inmediato, bypass del LLM
                return "Anotación guardada y vinculada."
        
        # --- AMPHIBIOUS CORTEX (Ojos Anfibios) ---
        visual_context = ""
        try:
            # Detectar entorno: Web vs Desktop
            # Intentamos usar pygetwindow para saber dónde estamos
            import pygetwindow as gw
            window = gw.getActiveWindow()
            title = window.title.lower() if window and window.title else ""
            
            # Heurística simple: Si dice Chrome/Edge/Firefox -> Web
            is_browser = any(b in title for b in ["chrome", "edge", "firefox", "brave", "opera"])
            
            scan = None
            source_type = "UNKNOWN"
            
            if is_browser:
                source_type = "BROWSER"
                from app.skills.tools.web import web_scan_context
                scan = web_scan_context(call_id="amphibious_web")
            else:
                source_type = "DESKTOP"
                # Solo escaneamos desktop si es una app real (no vacío)
                if title:
                    from app.skills.tools.desktop import desktop_scan_context
                    scan = desktop_scan_context(call_id="amphibious_desktop")

            # Inyectar si hubo éxito
            if scan and scan.success and "No active" not in scan.result:
                 log.info(f"👁️ Contexto {source_type} detectado: {title}")
                 visual_context = f"--- {source_type} CONTEXT (Visible App: {title}) ---\n{scan.result}\n--- END CONTEXT ---\n\n"

        except Exception as e:
            log.warning(f"Amphibious error: {e}")

        final_text = visual_context + user_text
        
        tools_def = registry.get_definitions()
        self.history.append({"role": "user", "content": final_text})
        self._trim_history() 
        self._emit("agent.user_input", {"text": user_text}) # Emitimos solo lo que dijo el usuario para UI limpia

        loops = 0
        while loops < self.max_tool_loops:
            # Check for cancellation
            if self._check_cancelled():
                log.warning("Agent processing cancelled by user")
                self._cleanup_incomplete_tool_calls()
                return "Operación cancelada."
            
            loops += 1
            self._emit("agent.think", {"loop": loops})
            
            # Validate history before LLM call to prevent API 400 errors
            self._validate_history()
            
            try:
                resp = self.llm.generate_reply(self.history, tools=tools_def)
            except Exception as e:
                log.error(f"Error LLM: {e}")
                return f"Error cerebral: {e}"

            msg_content = resp.content or ""
            assistant_msg = {"role": "assistant", "content": msg_content}
            
            if resp.tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in resp.tool_calls
                ]
            
            self.history.append(assistant_msg)

            if not resp.tool_calls:
                self._emit("agent.final", {"text": msg_content})
                return msg_content

            for tc in resp.tool_calls:
                # Check for cancellation before executing each tool
                if self._check_cancelled():
                    log.warning(f"Skipping tool {tc.name} due to cancellation")
                    # Add error response for this tool call
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "ERROR: Operation cancelled by user"
                    })
                    continue
                
                internal_call = ToolCall(id=tc.id, tool_name=tc.name, arguments=tc.arguments)
                verdict = policy.evaluate(internal_call)
                
                try:
                    if verdict.decision == Decision.DENY:
                        res = ToolResult(call_id=tc.id, success=False, error=f"Policy DENY: {verdict.reason}")
                    else:
                        self._emit("agent.tool_call", {"tool": tc.name})
                        res = registry.execute(internal_call)
                except Exception as e:
                    # CRITICAL: Si la tool falla (timeout, interrupción, etc), 
                    # DEBEMOS agregar una respuesta al historial para evitar Error 400
                    log.error(f"Tool execution error for {tc.name}: {e}")
                    res = ToolResult(call_id=tc.id, success=False, error=f"Tool execution failed: {e}")
                
                self.history.append({"role": "tool", "tool_call_id": tc.id, "content": res.to_text()})

        return "Límite de ejecución alcanzado."

agent = Agent()