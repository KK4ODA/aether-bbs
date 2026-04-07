"""
bbs/rate_limiter.py

In-memory sliding-window rate limiter.
No database writes — purely in-memory for performance.
Resets on BBS restart (acceptable for this use case).
"""

import time
import threading
from collections import defaultdict, deque
from typing import Optional

from core.util import base_callsign


# Default rate limit profiles: (max_count, window_seconds)
RATE_LIMITS = {
    "connect":        (10,  3600),    # 10 connections per hour
    "auth_failure":   (5,   3600),    # 5 failed auths per hour
    "message_send":   (20,  3600),    # 20 messages per hour
    "bulletin_post":  (5,   3600),    # 5 bulletins per hour
    "file_upload":    (10,  3600),    # 10 uploads per hour
    "register":       (3,   86400),   # 3 registration attempts per day
    "command":        (120, 60),      # 120 commands per minute
}


class RateLimiter:
    """
    Per-callsign sliding-window rate limiter.
    Thread-safe.  All counters are in-memory.
    """

    def __init__(self, multiplier: float = 1.0):
        """
        Parameters
        ----------
        multiplier : float
            Applied to all max_count thresholds.
            < 1.0 = stricter, > 1.0 = more permissive.
        """
        self._lock = threading.Lock()
        self._multiplier = multiplier
        # callsign -> event_name -> deque of timestamps
        self._counters: dict[str, dict[str, deque]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=200))
        )

    @property
    def multiplier(self) -> float:
        return self._multiplier

    @multiplier.setter
    def multiplier(self, value: float):
        self._multiplier = max(0.1, value)

    def check(self, callsign: str, event: str,
              max_count: Optional[int] = None,
              window_seconds: Optional[int] = None) -> tuple[bool, int]:
        """
        Check if callsign is within rate limit for event type.
        Returns (allowed, current_count_in_window).

        If max_count / window_seconds are not provided, uses defaults
        from RATE_LIMITS for the given event name.
        """
        base = base_callsign(callsign)
        now = time.monotonic()

        if max_count is None or window_seconds is None:
            defaults = RATE_LIMITS.get(event, (100, 3600))
            max_count = max_count or defaults[0]
            window_seconds = window_seconds or defaults[1]

        # Apply multiplier to max_count
        effective_max = max(1, int(max_count * self._multiplier))
        cutoff = now - window_seconds

        with self._lock:
            q = self._counters[base][event]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= effective_max:
                return False, len(q)
            q.append(now)
            return True, len(q)

    def reset(self, callsign: str, event: Optional[str] = None):
        """Reset counters for a callsign."""
        base = base_callsign(callsign)
        with self._lock:
            if event:
                self._counters[base].pop(event, None)
            else:
                self._counters.pop(base, None)
