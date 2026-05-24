"""
ArthurOS Runtime - Lifecycle
----------------------------
Gestión centralizada del arranque (Startup) y apagado (Shutdown).

Objetivo:
- Evitar que servicios queden colgados.
- Publicar eventos de inicio/apagado al bus.
- Manejar señales (Ctrl+C) cuando corre como CLI.

Nota: Si esto se usa dentro de una GUI, el manejo de signals puede ser diferente.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from types import FrameType
from typing import Optional

from app.contracts.events import SystemEvent
from app.core.config import settings
from app.core.logger import log
from app.runtime.bus import bus


class LifecycleManager:
    def __init__(self) -> None:
        self.is_running = False
        self._shutdown_event = threading.Event()

    def startup(self) -> None:
        """Inicializa todos los subsistemas en orden estricto."""
        if self.is_running:
            return

        log.info(f"🚀 Iniciando {settings.APP_NAME} (ENV={settings.ENV})...")

        # 1) Verificar integridad de auditoría (best-effort; fail-closed se decide luego)
        try:
            from app.security.audit import auditor
            if not auditor.verify_integrity():
                log.warning("⚠️ ALERTA: auditoría parece manipulada o corrupta.")
        except Exception as e:
            log.warning(f"No se pudo verificar auditoría (continúo): {e}")

        # 2) Publicar evento de inicio
        bus.publish(SystemEvent(name="app.startup", payload={"env": settings.ENV}, source="lifecycle"))

        # 3) Señales SOLO si estamos en el hilo principal (requisito de Python)
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._handle_signal)
                signal.signal(signal.SIGTERM, self._handle_signal)
            except Exception as e:
                log.debug(f"No se pudieron registrar señales (ok en GUI): {e}")
        else:
            log.debug("Lifecycle: no registro señales porque no estoy en el hilo principal.")

        self.is_running = True
        
        # 4) Iniciar Watchdog
        try:
            from app.runtime.watchdog import watchdog
            watchdog.start()
        except Exception as e:
            log.warning(f"No se pudo iniciar Watchdog: {e}")
            
        log.info("✅ Sistema listo para recibir órdenes.")

    def shutdown(self, reason: str = "User request", *, exit_process: bool = True, exit_code: int = 0) -> None:
        """Apagado grácil (Graceful Shutdown)."""
        if not self.is_running:
            return

        log.info(f"🛑 Secuencia de apagado: {reason}")

        # 1) Avisar a todos los componentes
        try:
            bus.publish(SystemEvent(name="app.shutdown", payload={"reason": reason}, source="lifecycle"))
        except Exception as e:
            log.debug(f"Error publicando app.shutdown: {e}")

        # 2) Dar tiempo a cleanup (DB, hilos, etc.)
        time.sleep(0.25)

        self.is_running = False
        
        # Detener Watchdog
        try:
            from app.runtime.watchdog import watchdog
            watchdog.stop()
        except: pass
        
        self._shutdown_event.set()
        log.info("👋 ArthurOS detenido correctamente.")

        if exit_process:
            raise SystemExit(exit_code)

    def _handle_signal(self, signum: int, frame: Optional[FrameType]) -> None:
        sig_name = getattr(signal.Signals(signum), "name", str(signum))
        self.shutdown(reason=f"Signal received: {sig_name}", exit_process=True, exit_code=0)

    def wait_for_shutdown(self) -> None:
        """Mantiene el hilo principal vivo hasta que se apague (loop eficiente)."""
        while self.is_running:
            self._shutdown_event.wait(timeout=1.0)


# Instancia global
lifecycle = LifecycleManager()
