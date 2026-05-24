from enum import Enum
from typing import Optional

class FailureType(Enum):
    SELECTOR_NOT_FOUND = "SELECTOR_NOT_FOUND"
    ELEMENT_NOT_VISIBLE = "ELEMENT_NOT_VISIBLE"
    INTERACTION_FAILED = "INTERACTION_FAILED"  # Click intercepted, etc.
    TIMEOUT = "TIMEOUT"
    NAVIGATION_ERROR = "NAVIGATION_ERROR"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    UNKNOWN = "UNKNOWN"

class FailureClassifier:
    @staticmethod
    def classify(error: Exception) -> FailureType:
        msg = str(error).lower()
        
        if "timeout" in msg:
            return FailureType.TIMEOUT
        if "selector" in msg or "no such element" in msg:
            return FailureType.SELECTOR_NOT_FOUND
        if "intercepted" in msg or "obscured" in msg:
            return FailureType.INTERACTION_FAILED
        if "visible" in msg or "display" in msg:
            return FailureType.ELEMENT_NOT_VISIBLE
        if "validation" in msg or "assert" in msg:
            return FailureType.VALIDATION_FAILED
            
        return FailureType.UNKNOWN

    @staticmethod
    def is_recoverable(failure_type: FailureType) -> bool:
        # System errors usually fatal; selectors/timeouts might be retried
        return failure_type in {
            FailureType.SELECTOR_NOT_FOUND,
            FailureType.ELEMENT_NOT_VISIBLE, 
            FailureType.INTERACTION_FAILED, 
            FailureType.TIMEOUT,
            FailureType.VALIDATION_FAILED
        }
