"""Wallhaven's 45-calls-per-minute budget, as a pure function.

The limiter says how long the caller must wait and never waits itself; the caller does the waiting with a
cancellable `stop_event.wait(n)` (invariant 12). It counts **API calls** only: thumbnails and full-resolution
images come from other hosts.
"""

from __future__ import annotations

from collections.abc import Iterable

CALLS_PER_MINUTE = 45
"""Wallhaven's documented limit for `wallhaven.cc/api`."""

WINDOW_SECONDS = 60.0
"""The minute the limit is counted over."""


def wait_needed(
    call_times: Iterable[float],
    *,
    now: float,
    limit: int = CALLS_PER_MINUTE,
    window: float = WINDOW_SECONDS,
) -> float:
    """Seconds to wait before another **API call** may be made, or zero if one may be made now.

    `call_times` and `now` are monotonic seconds, so a wall clock stepping backwards cannot empty the window.
    A sliding window, not a bucket that refills on the minute: a bucket would allow 90 calls in two seconds
    across a minute boundary.
    """
    inside = sorted(t for t in call_times if now - t < window)
    if len(inside) < limit:
        return 0.0
    # The call that must age out leaves `limit - 1` behind it: not the oldest if the limit was lowered.
    ages_out = inside[len(inside) - limit]
    return max(0.0, ages_out + window - now)


THUMBNAIL_GAP_SECONDS = 0.25
"""The fixed gap between two thumbnail fetches. A constant, never a setting.

`th.wallhaven.cc` has no published limit, so the only promise is spacing: about four a second, like one person
browsing.
"""


def gap_needed(last_fetch: float | None, *, now: float, gap: float = THUMBNAIL_GAP_SECONDS) -> float:
    """Seconds to wait before the next thumbnail fetch, or zero. `None` means nothing has been fetched yet.

    The thumbnail hosts' counterpart to `wait_needed`: a gap, not a window, because there is nothing published
    to count against.
    """
    if last_fetch is None:
        return 0.0
    return max(0.0, last_fetch + gap - now)
