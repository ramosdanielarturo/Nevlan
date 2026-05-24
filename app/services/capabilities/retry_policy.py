from dataclasses import dataclass
from enum import Enum
import time

class RetryStrategy(Enum):
    IMMEDIATE = "IMMEDIATE" 
    EXPONENTIAL_BACKOFF = "EXPONENTIAL_BACKOFF"
    LINEAR_BACKOFF = "LINEAR_BACKOFF"

@dataclass
class RetryConfig:
    max_attempts: int = 3
    strategy: RetryStrategy = RetryStrategy.IMMEDIATE
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 10.0

class RetryPolicy:
    def __init__(self, config: RetryConfig = None):
        self.config = config or RetryConfig()
        self.attempts = {}  # key -> count

    def can_retry(self, key: str) -> bool:
        count = self.attempts.get(key, 0)
        return count < self.config.max_attempts

    def record_attempt(self, key: str):
        self.attempts[key] = self.attempts.get(key, 0) + 1

    def get_delay(self, key: str) -> float:
        count = self.attempts.get(key, 0)
        if count == 0:
            return 0
            
        if self.config.strategy == RetryStrategy.IMMEDIATE:
            return 0
        elif self.config.strategy == RetryStrategy.LINEAR_BACKOFF:
            delay = self.config.base_delay_seconds * count
            return min(delay, self.config.max_delay_seconds)
        elif self.config.strategy == RetryStrategy.EXPONENTIAL_BACKOFF:
            delay = self.config.base_delay_seconds * (2 ** (count - 1))
            return min(delay, self.config.max_delay_seconds)
        return 0

    def wait(self, key: str):
        delay = self.get_delay(key)
        if delay > 0:
            time.sleep(delay)
