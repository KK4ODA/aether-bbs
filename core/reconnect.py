"""
core/reconnect.py

One retry loop for every link the BBS keeps up: the VARA/Aether modem,
the AGWPE (Soundmodem) link, FLrig and the APRS-IS server.
"""

import time
from typing import Callable, Optional


class Backoff:
    """Delays of *initial*, *initial*×*factor*, … capped at *maximum*."""

    def __init__(self, initial: float, maximum: float, factor: float = 2.0):
        self.initial = initial
        self.maximum = maximum
        self.factor = factor
        self._delay = initial

    def next(self) -> float:
        """The delay to wait now; the following one grows."""
        delay = self._delay
        self._delay = min(self._delay * self.factor, self.maximum)
        return delay

    def reset(self) -> None:
        self._delay = self.initial


def retry(attempt: Callable[[], bool], backoff: Backoff, *,
          should_stop: Callable[[], bool] = lambda: False,
          wait: Optional[Callable[[float], object]] = None,
          max_attempts: Optional[int] = None,
          wait_first: bool = False,
          on_retry: Optional[Callable[[int, float], None]] = None) -> bool:
    """Call *attempt* until it returns True; wait a *backoff* delay between
    tries (and before the first one if *wait_first*).

    Stops early, returning False, when *should_stop* is true (checked
    before every try and every wait) or after *max_attempts* tries (None:
    no limit).  *wait* defaults to time.sleep; pass an Event's wait so a
    stop interrupts it.  *on_retry(tries_so_far, delay)* runs before each
    wait after a failed try, e.g. to log it."""
    if wait is None:
        wait = lambda delay: time.sleep(delay)   # noqa: E731 (looked up at call time)
    tries = 0
    if wait_first and not should_stop():
        wait(backoff.next())
    while not should_stop():
        tries += 1
        if attempt():
            return True
        if max_attempts is not None and tries >= max_attempts:
            return False
        if should_stop():
            return False
        delay = backoff.next()
        if on_retry:
            on_retry(tries, delay)
        wait(delay)
    return False
