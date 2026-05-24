"""
ArthurOS Telemetry - Metrics Service
------------------------------------
Servicio para registrar y consultar métricas de uso de herramientas.
Permite al Agente "conocerse a sí mismo" (Self-Health Awareness).
"""
import time
import threading
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class ToolMetric:
    tool_name: str
    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_duration_ms: float = 0
    last_called_at: Optional[datetime] = None
    last_error: Optional[str] = None
    recent_errors: List[str] = field(default_factory=list)  # Keep last 5 errors

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0: return 1.0
        return self.success_count / self.total_calls

    @property
    def avg_duration_ms(self) -> float:
        if self.total_calls == 0: return 0.0
        return self.total_duration_ms / self.total_calls

class TelemetryService:
    def __init__(self):
        self._metrics: Dict[str, ToolMetric] = {}
        self._lock = threading.Lock()
        self._start_time = datetime.now(timezone.utc)

    def record_tool_execution(self, tool_name: str, success: bool, duration_ms: float, error: Optional[str] = None):
        """Registra una ejecución de herramienta."""
        with self._lock:
            if tool_name not in self._metrics:
                self._metrics[tool_name] = ToolMetric(tool_name=tool_name)
            
            metric = self._metrics[tool_name]
            metric.total_calls += 1
            metric.total_duration_ms += duration_ms
            metric.last_called_at = datetime.now(timezone.utc)
            
            if success:
                metric.success_count += 1
            else:
                metric.failure_count += 1
                if error:
                    metric.last_error = str(error)[:200]  # Truncate
                    metric.recent_errors.append(str(error)[:100])
                    if len(metric.recent_errors) > 5:
                        metric.recent_errors.pop(0)

    def get_tool_stats(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Obtiene estadísticas de una herramienta específica."""
        with self._lock:
            if tool_name not in self._metrics:
                return None
            
            m = self._metrics[tool_name]
            return {
                "calls": m.total_calls,
                "success_rate": f"{m.success_rate:.1%}",
                "avg_latency": f"{m.avg_duration_ms:.1f}ms",
                "last_error": m.last_error
            }

    def get_system_health(self) -> Dict[str, Any]:
        """Retorna un reporte de salud del sistema."""
        with self._lock:
            uptime = datetime.now(timezone.utc) - self._start_time
            
            # Top failed tools
            failed_tools = []
            for name, m in self._metrics.items():
                if m.failure_count > 0:
                    failed_tools.append({
                        "tool": name,
                        " failures": m.failure_count,
                        "rate": f"{m.success_rate:.1%}",
                        "last_error": m.last_error
                    })
            
            # Sort by failure count desc
            failed_tools.sort(key=lambda x: x[" failures"], reverse=True)

            return {
                "uptime": str(uptime).split('.')[0],
                "total_tools_tracked": len(self._metrics),
                "problematic_tools": failed_tools[:5],  # Top 5 offending tools
                "status": "HEALTHY" if not failed_tools else "DEGRADED" if len(failed_tools) < 3 else "CRITICAL"
            }

# Instancia global
telemetry = TelemetryService()
