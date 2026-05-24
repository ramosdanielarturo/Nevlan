"""DEPRECATED: no usar este módulo como fuente de verdad.

La implementación vigente está en ``app.interfaces.desktop.recording_overlay``
(``ActionRecorderOverlay`` — eventos interpretados; finalizar, cancelar, anotar).

Este archivo reexporta el overlay activo sólo por compatibilidad con imports
antiguos. Preferir::
    from app.interfaces.desktop.recording_overlay import ActionRecorderOverlay
"""

from app.interfaces.desktop.recording_overlay import ActionRecorderOverlay

# Alias antiguo: algunos forks nombraban la clase RecordingOverlay
RecordingOverlay = ActionRecorderOverlay

__all__ = ["ActionRecorderOverlay", "RecordingOverlay"]
