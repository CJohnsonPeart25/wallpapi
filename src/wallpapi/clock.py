"""The clock, injected so that time is something tests move rather than wait for.

Invariant 5: every timestamp in the **Decision log** comes from here, never from `datetime.now()`.
`monotonic` is separate because the 45-calls-per-minute limiter must not be confused by a wall clock that
jumps.
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
