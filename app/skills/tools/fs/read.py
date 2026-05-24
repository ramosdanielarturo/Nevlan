"""ArthurOS Skills - File System (Read)

Operaciones de solo lectura.
Por defecto se restringen a la zona segura (settings.SANDBOX_PATH).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from app.contracts.tool_result import ToolResult
from app.core.config import settings
from app.skills.registry import registry
from app.skills.tools.fs._guard import safe_resolve


@registry.register(name="fs.read.list_directory")
def list_directory(call_id: str, path: str = ".", limit: int = 50) -> ToolResult:
    """Lista contenido de una carpeta (zona segura)."""
    try:
        limit = max(1, min(int(limit), 200))
        target = safe_resolve(path)
        if not target.exists():
            return ToolResult(call_id=call_id, success=False, error=f"Ruta no existe: {target}")
        if not target.is_dir():
            return ToolResult(call_id=call_id, success=False, error=f"No es carpeta: {target}")

        items: List[str] = []
        for i, item in enumerate(target.iterdir()):
            if i >= limit:
                items.append("…")
                break
            type_char = "📁" if item.is_dir() else "📄"
            items.append(f"{type_char} {item.name}")

        return ToolResult(call_id=call_id, result="\n".join(items))
    except PermissionError:
        return ToolResult(call_id=call_id, success=False, error="Permiso denegado.")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="fs.read.read_file")
def read_file(call_id: str, path: str, max_bytes: int = 1_000_000) -> ToolResult:
    """Lee texto de un archivo (zona segura)."""
    try:
        target = safe_resolve(path)
        if not target.exists():
            return ToolResult(call_id=call_id, success=False, error="Archivo no encontrado.")
        if target.is_dir():
            return ToolResult(call_id=call_id, success=False, error="La ruta es una carpeta.")

        if target.stat().st_size > int(max_bytes):
            return ToolResult(call_id=call_id, success=False, error=f"Archivo demasiado grande (>{max_bytes} bytes).")

        try:
            content = target.read_text(encoding="utf-8")
            return ToolResult(call_id=call_id, result=content)
        except UnicodeDecodeError:
            return ToolResult(call_id=call_id, success=False, error="Archivo binario o codificación no soportada.")

    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="fs.read.find_file")
def find_file(call_id: str, pattern: str, start_path: str = ".", max_results: int = 20) -> ToolResult:
    """Busca archivos por patrón (glob) dentro de la zona segura."""
    try:
        max_results = max(1, min(int(max_results), 200))
        start = safe_resolve(start_path)
        matches: List[str] = []
        count = 0

        for p in start.rglob(pattern):
            matches.append(str(p))
            count += 1
            if count >= max_results:
                break

        if not matches:
            return ToolResult(call_id=call_id, result="No se encontraron coincidencias.")

        return ToolResult(call_id=call_id, result=matches)

    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))
