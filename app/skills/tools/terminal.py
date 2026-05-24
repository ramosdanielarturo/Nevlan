"""ArthurOS Skills - Terminal (Shell)

Ejecuta comandos del sistema via PlatformDriver.
⚠️ Riesgo alto: debe pasar por PolicyManager (shell.execute).
"""

from __future__ import annotations

from app.contracts.tool_result import ToolResult
from app.platforms.detect import get_platform_driver
from app.core.logger import log
from app.skills.registry import registry


@registry.register(name="shell.execute")
def execute_command(call_id: str, command: str) -> ToolResult:
    """Ejecuta un comando de shell y devuelve STDOUT/STDERR."""
    try:
        driver = get_platform_driver()
        log.warning(f"SHELL EXEC: {command}")
        output = driver.run_shell_command(command)
        return ToolResult(call_id=call_id, result=output)
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=f"Fallo de comando: {str(e)}")
