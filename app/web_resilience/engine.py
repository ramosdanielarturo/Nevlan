from typing import Callable, Any, List
import time
from .taxonomy import WebFailureType
from .verifier import WebVerifier
from .narrator import WebNarrator
from app.core.logger import log

class WebResilienceEngine:
    def __init__(self):
        self.verifier = WebVerifier()
        self.narrator = WebNarrator()
        self.max_retries = 3

    def execute_with_resilience(self, action_name: str, strategies: List[Callable], check_fn: Callable[[], bool] = None) -> Any:
        """
        Executes an action using a list of strategies (callables).
        If one fails, tries the next.
        """
        last_error = None
        
        for i, strategy in enumerate(strategies):
            strategy_name = strategy.__name__ if hasattr(strategy, "__name__") else f"Strategy {i+1}"
            
            try:
                log.debug(f"Executing {action_name} using {strategy_name}")
                result = strategy()
                
                # Verify if check function provided
                if check_fn:
                    if not check_fn():
                        raise Exception("Verification failed after execution")
                
                # If we used a fallback (i > 0), narrate it
                if i > 0:
                    msg = self.narrator.narrate_success(strategy_name)
                    log.info(msg)
                    
                return result
                
            except Exception as e:
                last_error = e
                # TODO: Classify failure type here
                failure_type = WebFailureType.UNKNOWN
                msg = self.narrator.narrate_fallback(failure_type, strategies[i+1].__name__ if i+1 < len(strategies) else "None")
                log.warning(f"{strategy_name} failed: {e}. {msg}")
                time.sleep(1.0)
                
        raise last_error or Exception(f"All strategies failed for {action_name}")
