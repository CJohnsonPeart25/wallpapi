"""The downloader's pace: one thumbnail at a time, a fixed gap apart. Issue #44.

The same shape as the 45-per-minute limiter (invariant 11): a pure "how long must I wait" function over the
last fetch and the injected clock's monotonic time, and the caller does the waiting. Everything below is
arithmetic over numbers a test chose — no clock, no thread, no second of real time.
"""

from __future__ import annotations

import pytest

from wallpapi.ratelimit import gap_needed


def test_nothing_fetched_yet_means_no_wait() -> None:
    assert gap_needed(None, now=100.0) == 0.0


def test_straight_after_a_fetch_the_whole_gap_is_owed() -> None:
    assert gap_needed(10.0, now=10.0) == 0.25


def test_part_of_the_gap_already_gone_is_not_owed_again() -> None:
    assert gap_needed(10.0, now=10.1) == pytest.approx(0.15)  # pyright: ignore[reportUnknownMemberType]


def test_once_the_gap_has_passed_there_is_no_wait() -> None:
    assert gap_needed(10.0, now=10.25) == 0.0
    assert gap_needed(10.0, now=99.0) == 0.0
