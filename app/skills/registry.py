"""
ArthurOS Skills - Registry (OpenAI Fix)
---------------------------------------
Corrección: Reemplaza automáticamente los puntos (.) por guiones bajos (_)
para cumplir con la regex estricta de OpenAI '^[a-zA-Z0-9_-]+$'.
"""
import inspect
import json
import time
import functools
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, get_origin, get_args

from app.contracts.tool_call import ToolCall
from app.contracts.tool_result import ToolResult
from app.core.logger import log

_INTERNAL_PARAMS = {"call_id", "ctx", "context"}

@dataclass
class ToolDef:
    name: str
    description: str
    fn: Callable[..., Any]

class SkillRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolDef] = {}

    def register(self, name: str, description: str = None):
        """Decorador para registrar tools."""
        def decorator(fn):
            # --- FIX CRÍTICO PARA OPENAI ---
            # OpenAI prohíbe puntos. Convertimos 'fs.read.file' a 'fs_read_file'.
            raw_name = name or fn.__name__
            final_name = raw_name.replace(".", "_") 
            # -------------------------------
            
            final_desc = description or fn.__doc__ or "No description."
            
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                start_time = time.time()
                success = True
                error_msg = None
                
                try:
                    result = fn(*args, **kwargs)
                    
                    # Check logic success if it returns ToolResult
                    if isinstance(result, ToolResult) and not result.success:
                        success = False
                        error_msg = result.error
                    
                    return result
                except Exception as e:
                    success = False
                    error_msg = str(e)
                    raise e
                finally:
                    duration_ms = (time.time() - start_time) * 1000
                    try:
                        # Late import to avoid circular dep
                        from app.services.telemetry.metrics import telemetry
                        telemetry.record_tool_execution(final_name, success, duration_ms, error_msg)
                    except Exception as metric_err:
                        log.warning(f"Telemetry error: {metric_err}")

            # Guardamos la tool con el nombre corregido (con guiones bajos)
            # FIX: Guardamos el WRAPPER, no la fn original
            self._tools[final_name] = ToolDef(name=final_name, description=final_desc.strip(), fn=wrapper)
            
            return wrapper
        return decorator

    def get_definitions(self) -> List[Dict[str, Any]]:
        """Genera la lista de tools en formato OpenAI."""
        defs = []
        for tool in self._tools.values():
            defs.append({
                "type": "function",
                "function": {
                    "name": tool.name, # Aquí ya va con guiones bajos
                    "description": tool.description,
                    "parameters": self._get_json_schema(tool.fn),
                },
            })
        return defs

    def _get_json_schema(self, fn: Callable) -> Dict[str, Any]:
        """Convierte la firma de la función a JSON Schema estricto."""
        sig = inspect.signature(fn)
        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name in _INTERNAL_PARAMS:
                continue

            # Mapeo de tipos Python -> JSON
            ptype = "string" # Default
            ann = param.annotation
            if ann == int: ptype = "integer"
            elif ann == float: ptype = "number"
            elif ann == bool: ptype = "boolean"
            elif ann == list or get_origin(ann) == list: ptype = "array"
            elif ann == dict or get_origin(ann) == dict: ptype = "object"
            
            properties[param_name] = {"type": ptype}
            
            # Si no tiene valor por defecto, es obligatorio
            if param.default == inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False 
        }

    def execute(self, call: ToolCall) -> ToolResult:
        # El LLM nos devolverá 'fs_read_file', así que lo buscamos tal cual
        tool_name_sanitized = call.tool_name
        
        tool = self._tools.get(tool_name_sanitized)
        if not tool:
            # Fallback: intentar con guiones bajos si vino con puntos
            fallback_name = tool_name_sanitized.replace(".", "_")
            tool = self._tools.get(fallback_name)
            
        if not tool:
            return ToolResult(call_id=call.id, success=False, error=f"Tool no encontrada: {call.tool_name}")

        try:
            sig = inspect.signature(tool.fn)
            kwargs = call.arguments or {}
            
            # Inyección segura de call_id
            if "call_id" in sig.parameters:
                kwargs["call_id"] = call.id

            out = tool.fn(**kwargs)

            # Normalización de respuesta
            if isinstance(out, ToolResult):
                out.call_id = call.id
                return out
            
            return ToolResult(call_id=call.id, result=str(out))

        except Exception as e:
            log.exception(f"Tool error ({call.tool_name}): {e}")
            return ToolResult(call_id=call.id, success=False, error=str(e))

registry = SkillRegistry()