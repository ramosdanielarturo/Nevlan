"""
AgentWorker - Background Thread for Agent Processing
-----------------------------------------------------
Moves agent.process_user_input() off the main UI thread to prevent
UI freezing during agent "thinking" operations.

Features:
- Generation ID tracking for logical cancellation
- Signal-based communication with UI
- Exception handling and error reporting
"""

from PyQt6.QtCore import QThread, pyqtSignal
from app.core.logger import log
from app.brain.agent import agent


class AgentWorker(QThread):
    """
    Background worker for agent processing.
    
    This thread executes agent.process_user_input() without blocking
    the UI. Each worker has a generation ID that allows the main window
    to discard stale responses if the user has moved on to a new query.
    """
    
    # Signals
    started = pyqtSignal()  # Emitted when processing begins
    finished = pyqtSignal(str, int)  # (response_text, run_id)
    error = pyqtSignal(str)  # (error_message)
    
    def __init__(self, query: str, run_id: int):
        """
        Initialize the worker.
        
        Args:
            query: The user's input text to process
            run_id: Generation ID for this request (for cancellation tracking)
        """
        super().__init__()
        self.query = query
        self.run_id = run_id
        
    def run(self):
        """
        Execute agent processing in background thread.
        
        This method runs in a separate thread and should never directly
        update UI elements. All UI updates must go through signals.
        """
        try:
            # Notify that processing has started
            self.started.emit()
            
            log.info(f"AgentWorker [{self.run_id}] processing: {self.query[:50]}...")
            
            # Execute agent processing (this may take several seconds)
            response = agent.process_user_input(self.query)
            
            log.info(f"AgentWorker [{self.run_id}] completed")
            
            # Emit result with run ID for validation
            self.finished.emit(response, self.run_id)
            
        except Exception as e:
            log.error(f"AgentWorker [{self.run_id}] error: {e}")
            self.error.emit(str(e))
