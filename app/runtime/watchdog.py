import threading
import time
import os
import signal
from app.core.logger import log

class Watchdog:
    def __init__(self, timeout_seconds=60):
        self.timeout = timeout_seconds
        self._last_heartbeat = time.time()
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()
        log.info("Watchdog started")

    def heartbeat(self):
        self._last_heartbeat = time.time()

    def _monitor(self):
        while self._running:
            if time.time() - self._last_heartbeat > self.timeout:
                log.critical("Watchdog detected frozen state! Attempting recovery...")
                # In a real scenario, this might restart the agent process or alert user
                # For now, we just log it and maybe try to interrupt main thread
                # os.kill(os.getpid(), signal.SIGINT) 
                
            time.sleep(5)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join()

watchdog = Watchdog()
