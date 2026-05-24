"""
ArthurOS Security - Audit Logger
--------------------------------
Implementa un log "tamper-evident" (evidencia de manipulación) usando Hash Chaining.
Cada entrada contiene el hash de la entrada anterior. Si se modifica un registro
intermedio, todos los hashes siguientes serán inválidos.

Características:
- Append-only (solo agregar).
- Thread-safe (bloqueo al escribir).
- Hash SHA-256 encadenado.
- Recuperación tras reinicios (lee último hash).
- Verificación de integridad (recalcula toda la cadena).
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.logger import log


def _stable_dumps(obj: Any) -> str:
    """
    Serialización estable para hashing.
    Nota: Usamos separators con espacios (default-like) para mantener legibilidad
    y evitar sorpresas si alguien inspecciona el archivo a mano.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


class AuditLogger:
    GENESIS_HASH = "0" * 64
    FILE_NAME = "audit_chain.jsonl"

    def __init__(self, file_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()

        root = settings.AUDIT_PATH
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            # Si no podemos crear el directorio de auditoría, es un problema grave.
            log.critical(f"No se pudo crear directorio de auditoría: {root} ({e})")

        self.file_path = file_path or (root / self.FILE_NAME)
        self._last_hash = self._get_last_hash()

    def _get_last_hash(self) -> str:
        """Lee la última línea para recuperar el hash previo (recuperación ante reinicios)."""
        if not self.file_path.exists():
            return self.GENESIS_HASH

        try:
            # Lectura eficiente del final (últimos 8KB)
            with open(self.file_path, "rb") as f:
                f.seek(0, 2)
                end = f.tell()
                if end == 0:
                    return self.GENESIS_HASH
                size = min(end, 8192)
                f.seek(-size, 2)
                chunk = f.read()
                lines = chunk.splitlines()
                if not lines:
                    return self.GENESIS_HASH
                last_line = lines[-1].decode("utf-8", errors="replace").strip()
                if not last_line:
                    return self.GENESIS_HASH
                data = json.loads(last_line)
                return str(data.get("hash", self.GENESIS_HASH))
        except Exception as e:
            log.error(f"Error recuperando hash de auditoría: {e}")
            return self.GENESIS_HASH

    def _calculate_hash(self, content_str: str, prev_hash: str) -> str:
        payload = f"{prev_hash}|{content_str}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def log_event(
        self,
        actor: str,
        action: str,
        target: str,
        status: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Registra un evento crítico y retorna el hash del registro.
        Ej: log_event("user", "delete_file", "/var/passwords.txt", "denied")
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        entry_content: Dict[str, Any] = {
            "ts": timestamp,
            "actor": actor,
            "action": action,
            "target": target,
            "status": status,
            "details": details or {},
        }

        content_str = _stable_dumps(entry_content)

        with self._lock:
            current_hash = self._calculate_hash(content_str, self._last_hash)

            final_entry: Dict[str, Any] = {
                "prev_hash": self._last_hash,
                **entry_content,
                "hash": current_hash,
            }

            try:
                # Append-only
                with open(self.file_path, "a", encoding="utf-8") as f:
                    f.write(_stable_dumps(final_entry) + "\n")
                self._last_hash = current_hash
                return current_hash
            except Exception as e:
                # Fail-closed recomendado en sistemas críticos.
                log.critical(f"FALLO DE AUDITORÍA: No se pudo escribir log. {e}")
                raise

    def verify_integrity(self) -> bool:
        """
        Recorre toda la cadena para verificar si el archivo fue manipulado externamente.
        Retorna True si la cadena está intacta.
        """
        if not self.file_path.exists():
            return True

        prev_hash = self.GENESIS_HASH

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    entry = json.loads(line)

                    stored_hash = str(entry.get("hash", ""))
                    stored_prev = str(entry.get("prev_hash", ""))

                    # Validaciones mínimas de forma
                    if len(stored_hash) != 64 or len(stored_prev) != 64:
                        log.critical(f"CORRUPCIÓN DE AUDITORÍA en línea {line_num}: formato inválido.")
                        return False

                    # 1) Verificar enlace
                    if stored_prev != prev_hash:
                        log.critical(
                            f"CORRUPCIÓN DE AUDITORÍA en línea {line_num}: prev_hash no coincide."
                        )
                        return False

                    # 2) Verificar hash de contenido (sin prev/hash)
                    content = dict(entry)
                    content.pop("hash", None)
                    content.pop("prev_hash", None)

                    content_str = _stable_dumps(content)
                    calculated_hash = self._calculate_hash(content_str, prev_hash)

                    if calculated_hash != stored_hash:
                        log.critical(
                            f"MANIPULACIÓN DE AUDITORÍA en línea {line_num}: hash inválido."
                        )
                        return False

                    prev_hash = calculated_hash

            log.info("Verificación de integridad de auditoría: EXITOSA")
            return True

        except Exception as e:
            log.error(f"Error verificando integridad: {e}")
            return False


# Instancia global (singleton simple)
auditor = AuditLogger()
