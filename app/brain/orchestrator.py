import logging
from typing import Optional, Dict, Any, Union

from app.brain.command_router import CommandRouter, RouteDecision, RouteType
from app.services.capabilities.mission_matcher import MissionMatcher
from app.services.capabilities.skill_matcher import SkillMatcher
from app.services.capabilities.help_request import HelpRequestGenerator
from app.services.missions.player import MissionPlayer # Assuming this exists or will be updated
from app.services.missions.store import mission_store

log = logging.getLogger(__name__)

class Orchestrator:
    def __init__(self):
        self.router = CommandRouter()
        self.mission_matcher = MissionMatcher()
        self.skill_matcher = SkillMatcher()
        # self.player = MissionPlayer() # Instantiate when needed to avoid circular imports?
        
    def handle(self, text: str, context: Dict[str, Any] = None) -> Any:
        context = context or {}
        
        # 1. Route
        decision = self.router.route(text, context)
        log.info(f"Orchestrator routed '{text}' to {decision}")
        
        # 2. Execute based on route
        if decision.route_type == RouteType.CONTROL:
            return self._handle_control(decision)
            
        if decision.route_type == RouteType.MISSIONS_ADMIN:
            return self._handle_mission_admin(decision)
            
        if decision.route_type == RouteType.MISSION_RUN:
            return self._handle_mission_run(decision)
            
        if decision.route_type == RouteType.SKILL_RUN:
            return self._handle_skill_run(decision)

        if decision.route_type == RouteType.SCROLL:
            return self._handle_scroll(decision)
            
        if decision.route_type == RouteType.LLM:
            return self._handle_llm(text, context)
            
        return None

    def _handle_control(self, decision: RouteDecision):
        cmd = decision.target
        if cmd == "STOP":
            # Logic to stop everything
            log.info("STOPPING execution...")
            # EventBus.emit("system.stop")
            return "Deteniendo..."
        elif cmd == "SLEEP":
             log.info("Going to SLEEP...")
             return "Durmiendo..."
        elif cmd == "CLOSE":
             log.info("Closing Application...")
             return "Cerrando..."
        return "Comando desconocido."

    def _handle_mission_admin(self, decision: RouteDecision):
        # Could return a structured object that the Agent/UI interprets
        # For now return string or trigger tool directly?
        target = decision.target
        if target == "list":
             return "Listando misiones..." # Agent should call missions_list
        if target == "record":
             return "Iniciando grabación..." # Agent should call start_recording
        if target == "save":
             return "Guardando misión..." # Agent should call save_recording
        return "Acción de administración no reconocida."

    def _handle_mission_run(self, decision: RouteDecision):
        mission_name = decision.target
        # ambiguous = decision.metadata.get("ambiguous", False)
        
        # Start execution
        # self.player.play(mission_name)
        return f"Ejecutando misión: {mission_name}"

    def _handle_skill_run(self, decision: RouteDecision):
        skill = decision.target
        params = decision.metadata
        return f"Ejecutando habilidad: {skill} con parámetros {params}"

    def _handle_scroll(self, decision: RouteDecision) -> str:
        """
        Maneja comandos de scroll enviándolos a Web o Desktop.
        """
        target = decision.target # "reading_mode" or "scroll_action"
        meta = decision.metadata
        
        # 1. Determinar contexto (Web vs Desktop)
        # Por ahora heurístico simple: si hay navegador conectado en WebTool, priorizar web
        try:
            from app.skills.tools.web import _PAGE
            is_web = _PAGE is not None and not _PAGE.is_closed()
        except ImportError:
            is_web = False
            
        tool_name = ""
        args = {}
        
        # 2. Configurar argumentos
        if target == "reading_mode":
            tool_name = "web.scroll_mode_start" if is_web else "desktop.scroll_mode_start"
            if not is_web:
                 # Fallback desktop single scroll per iteration
                 # TODO: Implement desktop scroll mode proper
                 tool_name = "desktop.scroll"
                 args = {"direction": "down", "amount": 20, "speed": "slow"}
            else:
                 args = {"speed": meta.get("speed", "slow")}

        else:
            # Standard scroll
            if is_web:
                tool_name = "web.scroll"
                args = {
                    "direction": meta.get("direction", "down"),
                    "amount": meta.get("amount", 1),
                    "unit": meta.get("unit", "page"),
                    "speed": meta.get("speed", "normal")
                }
            else:
                tool_name = "desktop.scroll"
                amount = meta.get("amount", 1)
                # Adjust params for desktop
                if meta.get("unit") == "page": amount *= 3 
                elif meta.get("unit") == "px": amount = int(amount / 100)
                
                args = {
                    "direction": meta.get("direction", "down"),
                    "amount": max(1, int(amount)),
                    "speed": meta.get("speed", "normal")
                }

        # 3. Ejecutar tool directamente
        from app.skills.registry import registry
        
        try:
            func = registry.get(tool_name)
            if func:
                result = func(call_id="orchestrator_scroll", **args)
                return f"Scroll ejecutado: {result.result}"
            else:
                return f"Tool {tool_name} no encontrado (¿falta implementar?)."
        except Exception as e:
            return f"Error ejecutando scroll: {e}"

    def _handle_llm(self, text: str, context: Dict[str, Any]):
        # Return special signal to let the LLM agent handle it
        return "LLM_FALLBACK"
