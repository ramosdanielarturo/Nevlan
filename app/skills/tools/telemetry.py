from app.contracts.tool_result import ToolResult
from app.skills.registry import registry
from app.services.telemetry.metrics import telemetry

@registry.register(name="telemetry.get_health")
def telemetry_get_health(call_id: str) -> ToolResult:
    """
    Obtiene un reporte de salud del sistema y estadísticas de herramientas.
    Usa esto si notas que tus acciones fallan frecuentemente para diagnosticar qué tool está rota.
    """
    try:
        health = telemetry.get_system_health()
        return ToolResult(call_id=call_id, result=health)
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))
