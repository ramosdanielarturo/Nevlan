"""
ArthurOS Run Ledger
-------------------
Bitácora persistente de las ejecuciones de misiones (Job Instances).
Garantiza trazabilidad completa de corridas manuales y disparadas por eventos.
"""
from typing import List
import json
from pathlib import Path

from app.core.config import settings
from app.contracts.ledger import RunRecord, JobInstance
from app.core.logger import log

class LedgerStore:
    def __init__(self):
        self.ledger_dir = settings.VAR_PATH / "ledger"
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        
        self.runs_file = self.ledger_dir / "runs.jsonl"
        self.jobs_file = self.ledger_dir / "jobs.jsonl"

    def log_job(self, job: JobInstance):
        """Registra la intención o encolamiento del trabajo."""
        try:
            with open(self.jobs_file, "a", encoding="utf-8") as f:
                f.write(job.model_dump_json() + "\n")
            log.debug(f"Ledger: Logged Job {job.id} for Mission {job.mission_id}")
        except Exception as e:
            log.error(f"Ledger error logging job: {e}")

    def log_run(self, record: RunRecord):
        """Documenta el resultado final post-ejecución."""
        try:
            with open(self.runs_file, "a", encoding="utf-8") as f:
                f.write(record.model_dump_json() + "\n")
            log.info(f"Ledger: Saved RunRecord {record.run_id} (Status: {record.status.value})")
        except Exception as e:
            log.error(f"Ledger error logging run: {e}")

    def get_runs(self, limit: int = 50) -> List[RunRecord]:
        """Obtiene el historial de ejecuciones más recientes."""
        if not self.runs_file.exists():
            return []
        
        runs = []
        try:
            with open(self.runs_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in reversed(lines[-limit:]):
                    if line.strip():
                        runs.append(RunRecord.model_validate_json(line))
        except Exception as e:
            log.error(f"Ledger read error: {e}")
        return runs

# Global singleton
ledger_store = LedgerStore()
