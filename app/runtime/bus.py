"""
ArthurOS Runtime - Event Bus
----------------------------
Implementación robusta del patrón Pub/Sub.
Permite que componentes (UI, Brain, Skills) se comuniquen sin dependencias directas.

Características:
- Un listener roto no rompe el bus (manejo de excepciones).
- Thread-safe (suscripción / publicación).
- Wildcard '*' para escuchar todo.
- Publicación síncrona para preservar orden (por ahora).
"""

from __future__ import annotations

import collections
import threading
from typing import Callable, DefaultDict, List

from app.contracts.events import SystemEvent
from app.core.logger import log

EventHandler = Callable[[SystemEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: DefaultDict[str, List[EventHandler]] = collections.defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        """Escuchar un evento específico."""
        if not event_name:
            raise ValueError("event_name no puede ser vacío")
        if handler is None:
            raise ValueError("handler no puede ser None")

        with self._lock:
            self._subscribers[event_name].append(handler)

        hname = getattr(handler, "__name__", repr(handler))
        log.debug(f"EventBus: suscrito a '{event_name}' -> {hname}")

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        """Dejar de escuchar."""
        with self._lock:
            handlers = self._subscribers.get(event_name)
            if not handlers:
                return
            try:
                handlers.remove(handler)
            except ValueError:
                return
            if not handlers:
                self._subscribers.pop(event_name, None)

    def publish(self, event: SystemEvent) -> None:
        """Publica un evento a todos los suscriptores (síncrono, preserva orden)."""
        handlers: List[EventHandler] = []
        with self._lock:
            handlers.extend(self._subscribers.get(event.name, []))
            handlers.extend(self._subscribers.get("*", []))

        for handler in handlers:
            try:
                handler(event)
            except Exception:
                # No propagamos para no romper al publicador ni a otros listeners.
                hname = getattr(handler, "__name__", repr(handler))
                log.exception(f"EventBus: error en handler {hname} para evento '{event.name}'")

    # Útil para tests
    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()


# Instancia global
bus = EventBus()
