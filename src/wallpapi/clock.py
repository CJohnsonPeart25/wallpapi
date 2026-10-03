"""The clock, injected so that time is something tests move rather than wait for.

Every timestamp in the **Decision log** comes from here, never from `datetime.now()`. `monotonic` is separate
so the rate limiter is not confused by a wall clock that jumps.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Protocol


class Clock(Protocol):
    """What the Core service needs of time."""

    def now(self) -> dt.datetime:
        """The current moment, always UTC-aware."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary fixed point, for measuring elapsed time."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def monotonic(self) -> float:
        return time.monotonic()
