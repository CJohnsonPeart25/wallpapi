"""The clock, injected so tests move time rather than wait for it. `monotonic` is separate so the rate limiter
ignores wall-clock jumps.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Protocol


class Clock(Protocol):
    """What the modules need of time."""

    def now(self) -> dt.datetime:
        """The current moment, always UTC-aware."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary fixed point."""
        ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def monotonic(self) -> float:
        return time.monotonic()


def iso_utc(at: dt.datetime) -> str:
    """A moment as the database keeps it: an ISO 8601 UTC string (invariant 5). A naive moment could be any
    zone, so it is refused.
    """
    if at.tzinfo is None:
        raise ValueError("a stored timestamp must be UTC-aware")
    return at.astimezone(dt.UTC).isoformat()
