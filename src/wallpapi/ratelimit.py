"""Wallhaven's 45-calls-per-minute budget, as pure functions over call timestamps.

The limiter says how long to wait and never waits itself, so the caller's wait stays a cancellable
`stop_event.wait(n)` (invariant 12).
"""

from __future__ import annotations

from collections.abc import Iterable

CALLS_PER_MINUTE = 45

WINDOW_SECONDS = 60.0


def wait_needed(
    call_times: Iterable[float],
    *,
    now: float,
    limit: int = CALLS_PER_MINUTE,
    window: float = WINDOW_SECONDS,
) -> float:
    """Seconds to wait before another **API call** may be made, or zero. A sliding window, not a per-minute
    bucket.
    """
    inside = sorted(t for t in call_times if now - t < window)
    if len(inside) < limit:
        return 0.0
    # The call that must age out leaves `limit - 1` behind it: not the oldest if the limit was lowered.
    ages_out = inside[len(inside) - limit]
    return max(0.0, ages_out + window - now)


THUMBNAIL_GAP_SECONDS = 0.25
"""The fixed gap between thumbnail fetches: a constant, never a setting."""


def gap_needed(last_fetch: float | None, *, now: float, gap: float = THUMBNAIL_GAP_SECONDS) -> float:
    """Seconds to wait before the next thumbnail fetch, or zero. A gap, not a window."""
    if last_fetch is None:
        return 0.0
    return max(0.0, last_fetch + gap - now)
