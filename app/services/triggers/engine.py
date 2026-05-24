"""
ArthurOS Trigger Engine (Scheduler & Event Listener)
----------------------------------------------------
Demonio en background que orquesta "JobInstances" basados en 
TriggerDefinitions (Intervalos, Archivos nuevos, etc.).
"""
import time
import threading
from datetime import datetime, timezone
from typing import Dict, Any

from app.core.logger import log
from app.contracts.mission import TriggerType
from app.contracts.ledger import JobInstance, JobSource, JobStatus, RunRecord
from app.services.missions import mission_store
from app.services.missions.player import replay_mission
from app.services.missions.ledger import ledger_store

class TriggerEngine:
    def __init__(self):
        self._running = False
        self._thread = None
        self._last_run: Dict[str, float] = {} # trigger_id -> timestamp

    def start(self):
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Trigger Engine started.")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        log.info("Trigger Engine stopped.")

    def _loop(self):
        while self._running:
            try:
                self._evaluate_triggers()
            except Exception as e:
                log.error(f"Error in TriggerEngine loop: {e}")
            time.sleep(10) # Evalua cada 10 segundos

    def _evaluate_triggers(self):
        now = time.time()
        
        # Pull all missions to see if they have active triggers
        missions = mission_store.list_all()
        for mission in missions:
            if not mission.triggers: continue
            
            for trigger in mission.triggers:
                if not trigger.is_active: continue
                
                # Evaluate interval schedules
                if trigger.type == TriggerType.SCHEDULE:
                    interval_sec = trigger.config.get("interval_seconds", 3600)
                    last = self._last_run.get(trigger.id, 0)
                    
                    if now - last >= interval_sec:
                        self._last_run[trigger.id] = now
                        self._dispatch_job(mission, trigger, JobSource.SCHEDULE)
                
                # Evaluate File Events (naive polling for demo)
                elif trigger.type == TriggerType.FILE_EVENT:
                    import os
                    target_dir = trigger.config.get("directory")
                    if target_dir and os.path.exists(target_dir):
                        # Naive check: si hay algún archivo nuevo no procesado (simplificado)
                        last_check = self._last_run.get(trigger.id, 0)
                        files = os.listdir(target_dir)
                        new_files_found = any(os.path.getmtime(os.path.join(target_dir, f)) > last_check for f in files)
                        
                        if new_files_found:
                            self._last_run[trigger.id] = now
                            self._dispatch_job(mission, trigger, JobSource.EVENT_TRIGGER)

    def _dispatch_job(self, mission, trigger, source: JobSource):
        """Encola el job y lo ejecuta inmediatamente para esta PoC."""
        log.info(f"Trigger {trigger.id} disparado para Misión {mission.name}")
        
        job = JobInstance(
            mission_id=mission.id,
            version_snapshot=mission.version,
            source=source,
            trigger_id=trigger.id
        )
        ledger_store.log_job(job)
        
        # Ejecución asíncrona para no bloquear el engine
        threading.Thread(target=self._execute_and_record, args=(mission, job), daemon=True).start()

    def _execute_and_record(self, mission, job: JobInstance):
        start_time = datetime.now(timezone.utc)
        record = RunRecord(
            job_id=job.id,
            mission_id=mission.id,
            started_at=start_time,
            status=JobStatus.RUNNING,
            total_steps=len(mission.compiled_execution_graph)
        )
        
        try:
             execution = replay_mission(mission, auto_confirm=True)
             
             record.completed_at = datetime.now(timezone.utc)
             record.duration_ms = int((record.completed_at - start_time).total_seconds() * 1000)
             record.steps_executed = execution.steps_executed
             
             if execution.status.value == "success":
                 record.status = JobStatus.SUCCESS
             else:
                 record.status = JobStatus.FAILED
                 record.error_message = execution.error_details
                 
        except Exception as e:
             record.status = JobStatus.FAILED
             record.error_message = str(e)
             
        finally:
             ledger_store.log_run(record)

# Global singleton
trigger_engine = TriggerEngine()
