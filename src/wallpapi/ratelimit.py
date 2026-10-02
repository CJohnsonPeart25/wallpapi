"""Wallhaven's 45-calls-per-minute budget, as a pure function.

Invariant 11: the limiter says how long the caller must wait and never waits itself. The caller is the
**Pool** refill thread, whose every wait is a cancellable `stop_event.wait(n)` (invariant 12) — a limiter
that slept would be a limiter that could hang shutdown for a minute.

The limit applies to **API calls** (`wallhaven.cc/api`) and to nothing else. Thumbnails and full-resolution
images come from `th.wallhaven.cc` and `w.wallhaven.cc`, separate hosts, and counting them here would spend
the search budget on files that never touch it.
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

    `call_times` and `now` are monotonic seconds from the injected clock, never wall-clock times: a clock
    that steps backwards over an NTP correction would otherwise empty the window and hand out a free minute
    of calls.

    A sliding window rather than a bucket that refills on the minute. A bucket lets 45 calls land at 11:59:59
    and 45 more at 12:00:00, which is 90 calls in two seconds and exactly what the limit exists to stop.
    """
    inside = sorted(t for t in call_times if now - t < window)
    if len(inside) < limit:
        return 0.0
    # The call that has to age out is the one that leaves `limit - 1` behind it. With exactly `limit` calls
    # in the window that is the oldest; with more — a limit lowered while calls were in flight — it is
    # further along.
    ages_out = inside[len(inside) - limit]
    return max(0.0, ages_out + window - now)


THUMBNAIL_GAP_SECONDS = 0.25
"""The fixed gap between two of the downloader's thumbnail fetches (#44). A constant, never a setting.

`th.wallhaven.cc` is not the 45-per-minute API, but it sits behind DDoS protection with no published limit
(invariant 11), and it is the host the user's own browser loads thumbnails from when browsing Wallhaven.
One at a time and about four a second looks like one person browsing, and still covers a **Pool** of 500
in two or three minutes.
"""


def gap_needed(last_fetch: float | None, *, now: float, gap: float = THUMBNAIL_GAP_SECONDS) -> float:
    """Seconds to wait before the next thumbnail fetch, or zero if one may be made now.

    The thumbnail hosts' counterpart to `wait_needed`, and pure for the same reason: the caller is a
    background thread whose every wait is a cancellable `stop_event.wait(n)` (invariant 12). `last_fetch` and
    `now` are monotonic seconds from the injected clock; `None` means nothing has been fetched yet.

    A gap rather than a window. Nothing is published to count against, so the only promise is spacing:
    never two fetches closer together than `gap`.
    """
    if last_fetch is None:
        return 0.0
    return max(0.0, last_fetch + gap - now)
