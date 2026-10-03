"""The 45-calls-per-minute limiter. Issue #6.

Invariant 11: the limiter is a pure "how long must I wait" function over **API call** timestamps, and the
caller does the waiting. That is what makes it testable without a clock, a thread or a second of real time —
everything below is arithmetic over numbers a test chose.

The timestamps are monotonic seconds, not wall-clock times: a wall clock that jumps backwards over a
daylight-saving change or an NTP correction would otherwise hand out a minute's worth of free calls.
"""

from __future__ import annotations

from wallpapi.ratelimit import CALLS_PER_MINUTE, WINDOW_SECONDS, wait_needed


def test_no_calls_yet_means_no_wait() -> None:
    assert wait_needed((), now=0.0) == 0.0


def test_a_full_minute_of_calls_is_allowed_without_waiting() -> None:
    """Acceptance criterion: the client never exceeds 45 calls a minute — and never undershoots it either.

    Forty-five calls in the same instant is the worst shape the budget allows, and the forty-fifth of them
    still must not wait. A limiter that spaced calls evenly would pass "never exceeds" while quietly
    halving the refill's throughput.
    """
    calls = [0.0] * (CALLS_PER_MINUTE - 1)

    assert wait_needed(calls, now=0.0) == 0.0


def test_the_forty_sixth_call_waits_exactly_until_the_oldest_leaves_the_window() -> None:
    """The whole rule in one assertion: wait for the oldest call to age out, and not a moment longer."""
    calls = [0.0] * CALLS_PER_MINUTE

    assert wait_needed(calls, now=0.0) == WINDOW_SECONDS
    assert wait_needed(calls, now=20.0) == WINDOW_SECONDS - 20.0
    assert wait_needed(calls, now=WINDOW_SECONDS) == 0.0


def test_calls_older_than_the_window_do_not_count() -> None:
    """A sliding window, not a bucket that fills up for ever.

    Forty-five calls a full minute ago plus one just now is one call in the window, so the next is free.
    """
    aged = [0.0] * CALLS_PER_MINUTE

    assert wait_needed([*aged, 100.0], now=100.0) == 0.0


def test_the_wait_is_measured_from_the_oldest_call_still_inside_the_window() -> None:
    """Spread-out calls wait less than bunched ones, because the window is already draining.

    The first five of the fifty are outside the window at `now`, so the call that has to age out is the
    sixth — and the wait is measured from that one, not from the first.
    """
    calls = [float(n) for n in range(50)]

    # 50 calls at one per second; at t=65 the ones at 0..4 have aged out, leaving 45 in the window.
    assert wait_needed(calls, now=65.0) == 0.0
    # At t=64 there are 46 in the window, so the call at 5 must age out first: 5 + 60 - 64.
    assert wait_needed(calls, now=64.0) == 1.0


def test_unordered_timestamps_are_handled_rather_than_trusted() -> None:
    """The caller keeps a deque it appends to, so the order is ascending in practice.

    Sorting here anyway costs nothing at 45 entries and means the function cannot be made to hand out extra
    calls by a caller that recorded one late.
    """
    calls = [0.0] * (CALLS_PER_MINUTE - 1)

    assert wait_needed([*calls, 30.0], now=30.0) == WINDOW_SECONDS - 30.0
