"""
ArthurOS Services - Mission Store
--------------------------------
CRUD para misiones. Persistencia en var/missions/ como JSON.
"""
from __future__ import annotations

import json
import threading
import shutil
import os
from pathlib import Path
from typing import List, Optional, Dict, Any

from app.contracts.mission import Mission, mission_to_dict, mission_from_dict
from app.core.config import settings
from app.core.logger import log


class MissionStore:
    """
    Servicio de persistencia de misiones.
    - Load/save/delete operaciones atómicas.
    - búsqueda por nombre.
    - thread-safe.
    """
    
    def __init__(self, base_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self.base_path = base_path or (settings.VAR_PATH / "missions")
        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.critical(f"No se pudo crear directorio de misiones: {self.base_path} ({e})")
            raise
        if not os.environ.get("NEVLAN_QUIET_BENCH"):
            log.debug(f"MissionStore inicializado en: {self.base_path}")
    
    def restore_mission_json_from_backup(
        self, mission_id: str, backup_path: Path,
    ) -> bool:
        """Restaura el JSON de la misión desde un .bak creado con ``backup_mission_json``."""
        with self._lock:
            try:
                src = Path(backup_path)
                if not src.is_file():
                    log.error(f"Backup inexistente: {src}")
                    return False
                dest = self._get_mission_file(mission_id)
                shutil.copy2(src, dest)
                log.info(f"Misión {mission_id} restaurada desde {src.name}")
                return True
            except Exception as e:
                log.error(f"restore_mission_json_from_backup: {e}")
                return False

    def backup_mission_json(self, mission_id: str, tag: str = "backup") -> Optional[Path]:
        """Copia el JSON actual de la misión antes de migraciones (recompilar, etc.).
        Devuelve la ruta del .bak o None si no había archivo."""
        with self._lock:
            try:
                file_path = self._get_mission_file(mission_id)
                if not file_path.exists():
                    return None
                bak = file_path.parent / f"{file_path.stem}.bak-{tag}_{__import__('time').time_ns()}.json"
                shutil.copy2(file_path, bak)
                log.info(f"Backup misión JSON: {bak.name}")
                return bak
            except Exception as e:
                log.error(f"No se pudo respaldar misión {mission_id}: {e}")
                return None

    def _get_mission_file(self, mission_id: str) -> Path:
        """Retorna el path del archivo JSON de la misión buscando por ID interno si el nombre no coincide."""
        expected_path = self.base_path / f"{mission_id}.json"
        if expected_path.exists():
            return expected_path
            
        # Resiliencia: Buscar en otros .json por si tiene nombre antiguo manual
        try:
            for file_path in self.base_path.glob("*.json"):
                if file_path == expected_path: continue
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        import json
                        data = json.load(f)
                    if data.get("id") == mission_id:
                        return file_path
                except Exception:
                    continue
        except Exception:
            pass
            
        return expected_path
    
    def save(self, mission: Mission) -> None:
        """Guarda (o actualiza) una misión. Atómico."""
        with self._lock:
            try:
                file_path = self._get_mission_file(mission.id)
                data = mission_to_dict(mission)
                
                # Escribe a temp primero, luego renames (atomicidad)
                temp_path = file_path.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False, default=str)
                
                temp_path.replace(file_path)
                log.debug(f"Misión guardada: {mission.name} ({mission.id})")
            except Exception as e:
                log.error(f"Error guardando misión {mission.name}: {e}")
                raise
    
    def load(self, mission_id: str) -> Optional[Mission]:
        """Carga una misión por ID. None si no existe."""
        with self._lock:
            try:
                file_path = self._get_mission_file(mission_id)
                if not file_path.exists():
                    return None
                
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                mission = mission_from_dict(data)
                # Saneamos thumbnails huérfanos (paths a assets/temp/
                # que ya no existen, PNGs corruptos, etc.). Sin esto,
                # cualquier consumidor que pinte el thumbnail puede
                # crashear con access violation en Qt — el bug del
                # MissionReviewDialog del 2026-05-03.
                try:
                    from app.services.missions.asset_validation import (
                        sanitize_mission_image_refs,
                    )
                    n = sanitize_mission_image_refs(mission)
                    if n:
                        log.debug(
                            f"MissionStore.load: {n} image_ref huérfanos "
                            f"saneados en {mission_id}"
                        )
                except Exception as _e:
                    log.debug(f"sanitize_mission_image_refs falló en load: {_e}")
                return mission
            except Exception as e:
                log.error(f"Error cargando misión {mission_id}: {e}")
                return None
    
    def find_by_name(self, name: str) -> Optional[Mission]:
        """Busca una misión por nombre exacto (case-insensitive)."""
        missions = self.list_all()
        target_name = name.lower()
        for mission in missions:
            if mission.name.lower() == target_name:
                return mission
        return None
    
    def list_all(self) -> List[Mission]:
        """Lista todas las misiones guardadas."""
        with self._lock:
            missions = []
            try:
                for file_path in self.base_path.glob("*.json"):
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        mission = mission_from_dict(data)
                        missions.append(mission)
                    except Exception as e:
                        log.warning(f"Error cargando misión de {file_path}: {e}")
                        continue
            except Exception as e:
                log.error(f"Error listando misiones: {e}")
            
            return missions
    
    def list_by_status(self, status: str) -> List[Mission]:
        """Lista misiones por estado."""
        return [m for m in self.list_all() if m.status.value == status]
    
    def delete(self, mission_id: str) -> bool:
        """Elimina una misión y sus assets por completo."""
        with self._lock:
            success = False
            try:
                # Borrar archivo json
                file_path = self._get_mission_file(mission_id)
                if file_path.exists():
                    file_path.unlink()
                    log.debug(f"Misión eliminada: {mission_id}")
                    success = True
                
                # Borrar carpeta de assets
                assets_dir = self.base_path / "assets" / mission_id
                if assets_dir.exists():
                    shutil.rmtree(assets_dir)
                    log.debug(f"Assets de misión {mission_id} eliminados por completo.")
                    
                return success
            except Exception as e:
                log.error(f"Error eliminando misión {mission_id}: {e}")
                return False
                
    def edit_mission_step(self, mission_id: str, step_id: str, new_data: dict) -> bool:
        """Actualiza un solo nodo (compiled step) y recalcula la misión."""
        mission = self.load(mission_id)
        if not mission:
            return False
            
        step_updated = False
        for i, step in enumerate(mission.compiled_execution_graph):
            if step.id == step_id:
                # Actualizar payload manteniendo el id
                new_step_data = step.model_dump()
                new_step_data.update(new_data)
                # Conservamos el ID intacto
                new_step_data['id'] = step_id 
                
                mission.compiled_execution_graph[i] = mission.compiled_execution_graph[i].__class__(**new_step_data)
                step_updated = True
                break
                
        if step_updated:
            mission.version += 1
            self.save(mission)
            return True
            
        return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Estadísticas del store."""
        missions = self.list_all()
        return {
            "total_missions": len(missions),
            "by_status": {
                status: len(self.list_by_status(status))
                for status in ["draft", "approved", "failed"]
            },
            "total_compiled_steps": sum(len(m.compiled_execution_graph) for m in missions),
            "store_path": str(self.base_path),
        }


# Instancia global
mission_store = MissionStore()
