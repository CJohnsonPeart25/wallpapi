"""The pacing functions (invariant 11): pure "how long must I wait" over monotonic timestamps; the caller
waits. Monotonic, because a wall clock jumping back over a daylight-saving change would hand out free calls.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from wallpapi.ratelimit import (
    CALLS_PER_MINUTE,
    THUMBNAIL_GAP_SECONDS,
    WINDOW_SECONDS,
    gap_needed,
    wait_needed,
)

FULL = [0.0] * CALLS_PER_MINUTE
ALL_BUT_ONE = [0.0] * (CALLS_PER_MINUTE - 1)
ONE_A_SECOND = [float(n) for n in range(50)]


@pytest.mark.parametrize(
    ("calls", "now", "wait"),
    [
        pytest.param((), 0.0, 0.0, id="no calls yet"),
        # 45 in one instant is the worst shape the budget allows, and the 45th still must not wait: spacing
        # calls evenly would pass "never exceeds" while halving the refill's throughput.
        pytest.param(ALL_BUT_ONE, 0.0, 0.0, id="a full minute is allowed without waiting"),
        pytest.param(FULL, 0.0, WINDOW_SECONDS, id="the 46th waits for the oldest to leave"),
        pytest.param(FULL, 20.0, WINDOW_SECONDS - 20.0, id="and not a moment longer"),
        pytest.param(FULL, WINDOW_SECONDS, 0.0, id="the oldest has left"),
        pytest.param([*FULL, 100.0], 100.0, 0.0, id="a sliding window, not a bucket"),
        # At t=65 the calls at 0..4 have aged out, leaving 45; at t=64 the call at 5 must age out first.
        pytest.param(ONE_A_SECOND, 65.0, 0.0, id="spread calls drain the window"),
        pytest.param(ONE_A_SECOND, 64.0, 1.0, id="measured from the oldest still inside"),
        pytest.param(
            [*ALL_BUT_ONE[1:], 30.0, 0.0], 30.0, WINDOW_SECONDS - 30.0, id="unordered is sorted, not trusted"
        ),
    ],
)
def test_the_api_call_window(calls: Sequence[float], now: float, wait: float) -> None:
    assert wait_needed(calls, now=now) == wait


@pytest.mark.parametrize(
    ("last", "now", "wait"),
    [
        pytest.param(None, 100.0, 0.0, id="nothing fetched yet"),
        pytest.param(10.0, 10.0, THUMBNAIL_GAP_SECONDS, id="straight after a fetch the whole gap is owed"),
        pytest.param(10.0, 10.1, THUMBNAIL_GAP_SECONDS - 0.1, id="part of the gap gone is not owed again"),
        pytest.param(10.0, 10.0 + THUMBNAIL_GAP_SECONDS, 0.0, id="the gap has passed"),
        pytest.param(10.0, 99.0, 0.0, id="long after"),
    ],
)
def test_the_thumbnail_gap(last: float | None, now: float, wait: float) -> None:
    """One thumbnail at a time, a fixed gap apart."""
    assert gap_needed(last, now=now) == pytest.approx(wait)  # pyright: ignore[reportUnknownMemberType]
