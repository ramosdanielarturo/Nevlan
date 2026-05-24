"""ArthurOS Skills - OS Tools

Herramientas generales para interactuar con el entorno.
Diseñadas para ser agnósticas al SO a través de app/platforms/.
"""

from __future__ import annotations

from pathlib import Path

from app.contracts.tool_result import ToolResult
from app.core.config import settings
from app.core.logger import log
from app.platforms.detect import get_platform_driver
from app.skills.registry import registry


@registry.register(name="os.get_common_paths")
def os_get_common_paths(call_id: str) -> ToolResult:
    """Devuelve rutas comunes del sistema (Escritorio, Documentos, Descargas, etc.).
    Úsalo cuando el usuario pida abrir 'Documentos', 'Descargas' o 'Escritorio' sin especificar ruta."""
    try:
        home = Path.home()
        paths = {
            "Desktop": str(home / "Desktop"),
            "Documents": str(home / "Documents"),
            "Downloads": str(home / "Downloads"),
            "Music": str(home / "Music"),
            "Pictures": str(home / "Pictures"),
            "Videos": str(home / "Videos"),
            "ArthurOS_Root": str(settings.BASE_PATH)
        }
        return ToolResult(call_id=call_id, result=paths)
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="os.get_system_info")
def os_get_system_info(call_id: str) -> ToolResult:
    """Devuelve información del sistema (OS, usuario, CPU, etc.)."""
    try:
        driver = get_platform_driver()
        info = driver.get_system_info()
        return ToolResult(call_id=call_id, result=info)
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="os.open")
def os_open(call_id: str, target: str) -> ToolResult:
    """Abre una aplicación o archivo usando el mecanismo por defecto del sistema.
    NOTA: Si el usuario pide 'Documentos' o 'Descargas', usa primero 'os.get_common_paths'
    para obtener la ruta absoluta correcta."""
    try:
        driver = get_platform_driver()
        if driver.open_file(target):
            return ToolResult(call_id=call_id, result=f"Abierto: {target}")
        return ToolResult(call_id=call_id, success=False, error=f"No se pudo abrir: {target}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="os.clipboard.read")
def os_clipboard_read(call_id: str) -> ToolResult:
    """Lee texto del portapapeles."""
    try:
        driver = get_platform_driver()
        content = driver.get_clipboard_text()
        return ToolResult(call_id=call_id, result=content or "")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="os.clipboard.write")
def os_clipboard_write(call_id: str, text: str) -> ToolResult:
    """Escribe texto al portapapeles."""
    try:
        driver = get_platform_driver()
        ok = driver.set_clipboard_text(text)
        if ok:
            return ToolResult(call_id=call_id, result="Texto copiado al portapapeles.")
        return ToolResult(call_id=call_id, success=False, error="Fallo al copiar al portapapeles.")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))
