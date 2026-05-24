"""ArthurOS Skills - File System (Write)

Operaciones de modificación.
⚠️ Alto riesgo: PolicyManager debe interceptar estas tools.

Diseño de seguridad:
- Por defecto operan SOLO dentro de settings.SANDBOX_PATH (safe_*).
- Variantes "anywhere_*" existen pero deben requerir confirmación estricta.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.contracts.tool_result import ToolResult
from app.core.config import settings
from app.core.logger import log
from app.skills.registry import registry
from app.skills.tools.fs._guard import safe_resolve, anywhere_resolve


@registry.register(name="fs.write.write_file")
def write_file(call_id: str, path: str, content: str, append: bool = False) -> ToolResult:
    """Escribe un archivo en SANDBOX (safe)."""
    try:
        target = safe_resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        target.write_text(content, encoding="utf-8") if mode == "w" else target.open(mode, encoding="utf-8").write(content)
        log.info(f"FS safe write: {target}")
        return ToolResult(call_id=call_id, result=f"Archivo guardado (sandbox): {target}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="fs.write.delete_path")
def delete_path(call_id: str, path: str) -> ToolResult:
    """Elimina archivo/carpeta en SANDBOX (safe)."""
    try:
        target = safe_resolve(path)
        if not target.exists():
            return ToolResult(call_id=call_id, success=False, error="Ruta no existe.")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        log.warning(f"FS safe delete: {target}")
        return ToolResult(call_id=call_id, result=f"Eliminado (sandbox): {target}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="fs.write.move")
def move_path(call_id: str, source: str, destination: str) -> ToolResult:
    """Mueve/renombra dentro de SANDBOX (safe)."""
    try:
        src = safe_resolve(source)
        dst = safe_resolve(destination)
        if not src.exists():
            return ToolResult(call_id=call_id, success=False, error="Origen no existe.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return ToolResult(call_id=call_id, result=f"Movido (sandbox): {src} -> {dst}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="fs.write.anywhere.write_file")
def anywhere_write_file(call_id: str, path: str, content: str, append: bool = False) -> ToolResult:
    """Escribe en cualquier ruta (alto riesgo)."""
    try:
        target = anywhere_resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)
        log.warning(f"FS ANYWHERE write: {target}")
        return ToolResult(call_id=call_id, result=f"Archivo guardado: {target}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="fs.write.anywhere.delete_path")
def anywhere_delete_path(call_id: str, path: str) -> ToolResult:
    """Elimina en cualquier ruta (alto riesgo)."""
    try:
        target = anywhere_resolve(path)
        if not target.exists():
            return ToolResult(call_id=call_id, success=False, error="Ruta no existe.")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        log.warning(f"FS ANYWHERE delete: {target}")
        return ToolResult(call_id=call_id, result=f"Eliminado: {target}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))


@registry.register(name="fs.write.anywhere.move")
def anywhere_move_path(call_id: str, source: str, destination: str) -> ToolResult:
    """Mueve/renombra en cualquier ruta (alto riesgo)."""
    try:
        src = anywhere_resolve(source)
        dst = anywhere_resolve(destination)
        if not src.exists():
            return ToolResult(call_id=call_id, success=False, error="Origen no existe.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        log.warning(f"FS ANYWHERE move: {src} -> {dst}")
        return ToolResult(call_id=call_id, result=f"Movido: {src} -> {dst}")
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))
