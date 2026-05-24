"""
ArthurOS Skills - Mission Tools
-------------------------------
Herramientas RPA para grabar, guardar, reproducir y gestionar misiones.
"""
from __future__ import annotations
import json
from typing import Optional, List, Dict, Any
from pathlib import Path

from app.contracts.tool_result import ToolResult
from app.core.logger import log
from app.skills.registry import registry
from app.services.missions import (
    mission_store,
    start_recording,
    stop_recording,
    pause_recording,
    resume_recording,
    get_recorder,
    replay_mission,
)
from app.contracts.mission import (
    AnnotationType, MissionStatus, ReplayStatus
)
from app.core.config import settings

# ============================================================================
# Grabación
# ============================================================================

@registry.register(name="missions.start_recording")
def missions_start_recording(call_id: str, name: Optional[str] = None) -> ToolResult:
    """Inicia grabación de una nueva misión."""
    try:
        mission = start_recording(name or "")
        return ToolResult(call_id=call_id, result={
            "mission_id": mission.id,
            "mission_name": mission.name,
            "status": mission.status.value,
            "message": f"Grabación iniciada: {mission.name}",
        })
    except Exception as e:
        log.error(f"Error iniciando grabación: {e}")
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="missions.pause_recording")
def missions_pause_recording(call_id: str) -> ToolResult:
    """Pausa la grabación actual."""
    try:
        pause_recording()
        return ToolResult(call_id=call_id, result={"message": "Grabación pausada"})
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="missions.resume_recording")
def missions_resume_recording(call_id: str) -> ToolResult:
    """Reanuda la grabación pausada."""
    try:
        resume_recording()
        return ToolResult(call_id=call_id, result={"message": "Grabación reanudada"})
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="missions.add_annotation")
def missions_add_annotation(call_id: str, annotation_type: str, value: str, target_step_id: Optional[str] = None) -> ToolResult:
    """Añade anotación durante grabación."""
    try:
        current = get_recorder()
        if not current or not current.is_recording():
             return ToolResult(call_id=call_id, success=False, error="No hay grabación activa")
        
        current.add_annotation(annotation_type.lower(), value, target_step_id)
        return ToolResult(call_id=call_id, result={"message": f"Anotación agregada: {value}"})
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

# Global to hold last stopped mission for saving
_LAST_MISSION = None

@registry.register(name="missions.stop_recording")
def missions_stop_recording(call_id: str) -> ToolResult:
    """Detiene la grabación y retorna la misión grabada."""
    global _LAST_MISSION
    try:
        from app.services.missions.recorder import stop_and_compile
        mission = stop_and_compile()
        
        _LAST_MISSION = mission  # Cache for saving
        
        return ToolResult(call_id=call_id, result={
            "mission_id": mission.id,
            "mission_name": mission.name,
            "events_recorded": len(mission.raw_trace),
            "compiled_steps": len(mission.compiled_execution_graph),
            "message": f"Grabación detenida. Esperando revisión (Tarjetas UI) para: {mission.name}"
        })
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

@registry.register(name="missions.save_recording")
def missions_save_recording(call_id: str, name: Optional[str] = None) -> ToolResult:
    """Guarda la misión recién grabada."""
    global _LAST_MISSION
    try:
        # If recording is active, stop and compile it first
        recorder = get_recorder()
        mission = None
        
        if recorder and recorder.is_recording():
             from app.services.missions.recorder import stop_and_compile
             mission = stop_and_compile()
             _LAST_MISSION = mission
        elif _LAST_MISSION:
             mission = _LAST_MISSION
        else:
             return ToolResult(call_id=call_id, success=False, error="No hay misión activa ni reciente para guardar")

        # Update name if provided
        if name:
            mission.name = name
            
        mission_store.save(mission)
        return ToolResult(call_id=call_id, result={"message": f"Misión guardada: {mission.name}"})
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

# ============================================================================
# Reproducción
# ============================================================================

@registry.register(name="missions.run")
def missions_run(call_id: str, name: str, variables: Optional[Dict[str, str]] = None, auto_confirm: bool = False) -> ToolResult:
    """Ejecuta una misión por nombre."""
    try:
        mission = mission_store.find_by_name(name)
        if not mission:
            return ToolResult(call_id=call_id, success=False, error=f"Misión no encontrada: {name}")
            
        execution = replay_mission(mission, auto_confirm=auto_confirm, variables=variables)
        
        # Publish event logic here (omitted for brevity, handled in player usually)
        
        if execution.status == ReplayStatus.SUCCESS:
             # Auto approve if success
             mission.status = MissionStatus.APPROVED
             mission.version += 1
             mission_store.save(mission)

        return ToolResult(call_id=call_id, result={
            "status": execution.status.value,
            "summary": execution.execution_summary,
            "verification": execution.execution_summary,
            "message": f"Ejecución finalizada: {execution.status.value}"
        })
    except Exception as e:
        return ToolResult(call_id=call_id, success=False, error=str(e))

# ============================================================================
# Gestión (CRUD)
# ============================================================================

@registry.register(name="missions.list")
def missions_list(call_id: str, status: Optional[str] = None) -> ToolResult:
    """Lista misiones disponibles."""
    missions = mission_store.list_by_status(status) if status else mission_store.list_all()
    summary = [{
        "name": m.name, 
        "status": m.status.value, 
        "raw_events": len(m.raw_trace),
        "compiled_steps": len(m.compiled_execution_graph),
        "aliases": list(getattr(m, 'aliases', []))
    } for m in missions]
    return ToolResult(call_id=call_id, result={"missions": summary})

@registry.register(name="missions.get")
def missions_get(call_id: str, name: str) -> ToolResult:
    """Obtiene detalles completos de una misión."""
    mission = mission_store.find_by_name(name)
    if not mission:
        return ToolResult(call_id=call_id, success=False, error=f"Misión no encontrada: {name}")
    
    # Return full detail
    return ToolResult(call_id=call_id, result=mission.model_dump(mode='json'))

@registry.register(name="missions.rename")
def missions_rename(call_id: str, old_name: str, new_name: str) -> ToolResult:
    """Renombra una misión."""
    mission = mission_store.find_by_name(old_name)
    if not mission:
        return ToolResult(call_id=call_id, success=False, error=f"Misión no encontrada: {old_name}")
    
    mission.name = new_name
    mission_store.save(mission)
    return ToolResult(call_id=call_id, result={"message": f"Misión renombrada a {new_name}"})

@registry.register(name="missions.delete")
def missions_delete(call_id: str, name: str) -> ToolResult:
    """Elimina una misión."""
    mission = mission_store.find_by_name(name)
    if not mission:
        return ToolResult(call_id=call_id, success=False, error=f"Misión no encontrada: {name}")
    
    mission_store.delete(mission.id)
    return ToolResult(call_id=call_id, result={"message": f"Misión eliminada: {name}"})

@registry.register(name="missions.add_alias")
def missions_add_alias(call_id: str, name: str, alias: str) -> ToolResult:
    """Agrega un alias a una misión."""
    mission = mission_store.find_by_name(name)
    if not mission:
        return ToolResult(call_id=call_id, success=False, error=f"Misión no encontrada: {name}")
    
    if alias not in mission.aliases:
        mission.aliases.append(alias)
        mission_store.save(mission)
        
    return ToolResult(call_id=call_id, result={"message": f"Alias '{alias}' agregado a {name}"})

@registry.register(name="missions.export")
def missions_export(call_id: str, name: str, format: str = "python") -> ToolResult:
    """Exporta la misión a código."""
    mission = mission_store.find_by_name(name)
    if not mission:
        return ToolResult(call_id=call_id, success=False, error=f"Misión no encontrada: {name}")

    export_path = settings.VAR_PATH / "exports"
    export_path.mkdir(parents=True, exist_ok=True)
    
    filename = f"{name.replace(' ', '_')}.{format.split('_')[0]}"
    file_path = export_path / filename

    content = f"# Export of mission {name}\n# Raw Events: {len(mission.raw_trace)}\n# Compiled Steps: {len(mission.compiled_execution_graph)}\n"
    # Basic stub for export logic
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return ToolResult(call_id=call_id, result={"path": str(file_path), "message": "Exportado exitosamente"})
