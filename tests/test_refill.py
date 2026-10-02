"""The **Pool** refill: when it runs, how fast, and what it does when Wallhaven says no. Issue #6, #15.

Driven a step at a time through the Core service, which is exactly why `refill_step` and `refill_wait` are
Core service methods rather than something inside the thread. No thread here, no sleeping, and the fake
clock means the whole 45-calls-per-minute budget is exercised in no real time at all.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import WallhavenUnreachable, catalogue_of
from wallpapi.core import (
    ERROR_BACKOFF_SECONDS,
    IDLE_RECHECK_SECONDS,
    Batch,
    BatchUnavailable,
)
from wallpapi.ratelimit import CALLS_PER_MINUTE, WINDOW_SECONDS


def test_the_refill_fills_the_pool_up_to_the_target_and_stops(db_path: Path) -> None:
    """Acceptance criterion: the refill keeps the **Pool** at a target size.

    Four per page and a target of ten, so it takes three calls to reach the target and the fourth step
    must not make one. Ten rather than twelve is deliberate: the **Pool** overshoots to a page boundary,
    which is fine — the target is where the refill stops asking, not a cap on what a page may bring.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(40), page_size=4, fill_pool=0)
    harness.core.update_settings(pool_target_size=10)

    harness.fill_pool(4)

    assert len(harness.wallhaven.searches) == 3
    assert harness.core.refill_status().pool_size == 12


def test_the_refill_idles_once_the_pool_is_at_target(db_path: Path) -> None:
    """A **Pool** at target means a long cancellable wait, not a spin on `SELECT COUNT(*)`."""
    harness = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=0)
    harness.core.update_settings(pool_target_size=5)

    harness.fill_pool(1)

    assert harness.core.refill_status().at_target
    assert harness.core.refill_wait() == IDLE_RECHECK_SECONDS


def test_the_refill_runs_again_once_the_pool_drops_below_target(db_path: Path) -> None:
    """The other half of the criterion: below target, the refill goes back to work.

    The **Pool** is taken below target by a **Ban**, which is the one **Verdict** that removes a
    **Wallpaper** from the draw for ever.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24), page_size=4, fill_pool=0)
    harness.core.update_settings(pool_target_size=8, batch_size=4)
    harness.fill_pool(3)
    assert harness.core.refill_wait() == IDLE_RECHECK_SECONDS

    harness.core.update_settings(pool_target_size=20)

    assert harness.core.refill_wait() == 0.0
    calls_before = len(harness.wallhaven.searches)
    harness.fill_pool(1)
    assert len(harness.wallhaven.searches) == calls_before + 1


def test_the_seed_is_carried_across_the_pages_of_one_walk(db_path: Path) -> None:
    """Acceptance criterion: random searches reuse Wallhaven's seed across pages.

    Wallhaven reshuffles on every call unless the seed is passed back, which would make page two of a walk
    as likely to hand back page one as anything new. The first call of a walk carries none — it is the call
    that asks for one — and every call after it carries what came back, on an increasing page number.
    """
    harness = make_harness(
        db_path, catalogue=catalogue_of(40), page_size=4, search_seed="seed-1", fill_pool=0
    )
    harness.core.update_settings(pool_target_size=1000)

    harness.fill_pool(3)

    assert [(s["page"], s["seed"]) for s in harness.wallhaven.searches] == [
        (1, None),
        (2, "seed-1"),
        (3, "seed-1"),
    ]


def test_a_fresh_walk_starts_from_a_fresh_seed(db_path: Path) -> None:
    """The **AGENTS.md** trap: reusing a seed across runs returns the same **Wallpapers**.

    The catalogue is one page long, so the second call comes back empty and ends the walk. The third call
    must start over — page one, no seed — rather than asking page three of a walk that is finished.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(4), page_size=4, search_seed="seed-1", fill_pool=0)
    harness.core.update_settings(pool_target_size=1000)

    harness.fill_pool(3)

    assert [(s["page"], s["seed"]) for s in harness.wallhaven.searches] == [
        (1, None),
        (2, "seed-1"),
        (1, None),
    ]


def test_reaching_the_target_ends_the_walk(db_path: Path) -> None:
    """A walk is one continuous sweep. Idling and then resuming is a new one, on a new seed."""
    harness = make_harness(
        db_path, catalogue=catalogue_of(40), page_size=4, search_seed="seed-1", fill_pool=0
    )
    harness.core.update_settings(pool_target_size=6)
    harness.fill_pool(3)

    harness.core.update_settings(pool_target_size=1000)
    harness.fill_pool(1)

    assert harness.wallhaven.searches[-1]["page"] == 1
    assert harness.wallhaven.searches[-1]["seed"] is None


def test_forty_five_calls_a_minute_are_allowed_and_the_forty_sixth_waits(db_path: Path) -> None:
    """Acceptance criterion: the client never exceeds 45 **API calls** a minute.

    The fake clock never advances on its own, so all 45 calls land in the same instant and the 46th has to
    wait the whole window. That is also the proof that no real time passed: a limiter that slept would have
    made this test take 45 seconds.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(400), page_size=1, fill_pool=0)
    harness.core.update_settings(pool_target_size=1000)

    harness.fill_pool(CALLS_PER_MINUTE)

    assert len(harness.wallhaven.searches) == CALLS_PER_MINUTE
    assert harness.core.refill_wait() == WINDOW_SECONDS


def test_the_budget_frees_up_as_the_oldest_calls_leave_the_window(db_path: Path) -> None:
    """The wait is until the oldest call ages out, and not a moment longer."""
    harness = make_harness(db_path, catalogue=catalogue_of(400), page_size=1, fill_pool=0)
    harness.core.update_settings(pool_target_size=1000)
    harness.fill_pool(CALLS_PER_MINUTE)

    harness.clock.advance(WINDOW_SECONDS - 10)
    assert harness.core.refill_wait() == 10.0

    harness.clock.advance(10)
    assert harness.core.refill_wait() == 0.0
    harness.fill_pool(1)
    assert len(harness.wallhaven.searches) == CALLS_PER_MINUTE + 1


def test_the_recorded_call_times_stay_bounded_however_long_the_refill_runs(db_path: Path) -> None:
    """wallpapi is meant to run for months. At 45 calls a minute that would be 65,000 floats a day.

    Only calls still inside the window can change what the limiter returns, so only those are kept. The
    bound is asserted directly, because "it is bounded" is the whole claim and nothing observable at the
    seam distinguishes 45 kept timestamps from 200.

    Second, subtler half: the clock does not move here. Trimming by age alone keeps everything when time
    stands still, which a fake clock does and a stalled monotonic clock could — so the count cap is what
    holds, and this is the test that would notice it being removed.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(400), page_size=1, fill_pool=0)
    harness.core.update_settings(pool_target_size=1000)

    harness.fill_pool(200)

    assert len(harness.wallhaven.searches) == 200
    assert len(harness.core.api_call_window()) <= CALLS_PER_MINUTE


def test_calls_that_have_aged_out_are_forgotten_rather_than_merely_ignored(db_path: Path) -> None:
    """The other half: with time moving, the window holds the recent calls and nothing older.

    Trimmed on the way in rather than inside `wait_needed`, which stays a pure function of what it is
    given (invariant 11).
    """
    harness = make_harness(db_path, catalogue=catalogue_of(400), page_size=1, fill_pool=0)
    harness.core.update_settings(pool_target_size=1000)

    harness.fill_pool(3)
    harness.clock.advance(WINDOW_SECONDS)
    harness.fill_pool(1)

    assert harness.core.api_call_window() == [WINDOW_SECONDS]


def test_a_429_backs_off_and_the_call_after_the_back_off_succeeds(db_path: Path) -> None:
    """Acceptance criterion: the client recovers from a 429.

    Wallhaven's own `Retry-After` is honoured over the default back-off — it knows when it will start
    answering again. The step during the back-off is not skipped by the Core service; the thread is what
    waits, so the test moves the clock instead.
    """
    harness = make_harness(
        db_path, catalogue=catalogue_of(24), rate_limited_calls=1, retry_after=17.0, fill_pool=0
    )

    harness.fill_pool(1)

    assert harness.core.refill_wait() == 17.0
    assert harness.core.refill_status().last_error
    harness.clock.advance(17.0)

    assert harness.core.refill_wait() == 0.0
    harness.fill_pool(1)
    assert harness.core.refill_status().pool_size == 24
    assert harness.core.refill_status().last_error is None


def test_a_429_without_a_retry_after_falls_back_to_the_default_back_off(db_path: Path) -> None:
    """Wallhaven need not say when to come back, and a guess of "immediately" would be the worst one."""
    harness = make_harness(db_path, catalogue=catalogue_of(24), rate_limited_calls=1, fill_pool=0)

    harness.fill_pool(1)

    assert harness.core.refill_wait() == ERROR_BACKOFF_SECONDS


def test_a_transport_failure_is_recorded_and_the_refill_keeps_going(db_path: Path) -> None:
    """#15: a failed **API call** never propagates out of the Core service and never kills the refill.

    `refill_step` returning rather than raising is the whole of it — the thread has no `except` of its own
    precisely because there is nothing for it to catch.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24), fail_from_call=1, fill_pool=0)

    harness.fill_pool(1)

    status = harness.core.refill_status()
    assert status.last_error
    assert status.last_error_at == harness.clock.now()
    assert status.pool_size == 0
    assert harness.core.refill_wait() == ERROR_BACKOFF_SECONDS


def test_a_failed_call_retries_the_same_page_rather_than_skipping_it(db_path: Path) -> None:
    """A page that was never fetched is a page whose **Wallpapers** were never seen.

    Advancing past it would silently thin the walk by a page every time the network hiccuped.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(40), page_size=4, fail_from_call=2, fill_pool=0)
    harness.core.update_settings(pool_target_size=1000)

    harness.fill_pool(3)

    assert [s["page"] for s in harness.wallhaven.searches] == [1, 2, 2]


def test_an_empty_pool_after_a_failed_refill_says_wallhaven_is_unreachable(db_path: Path) -> None:
    """The acceptance criterion #15 became: the **Batch unavailable** result says which kind of nothing.

    The error text and the time it happened travel with the result, because "it is not working" and "it
    stopped working at 11:30 with a connection refused" are different amounts of help.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24), fail_from_call=1, fill_pool=0)
    harness.fill_pool(1)

    result = harness.core.get_next_batch()

    assert isinstance(result, BatchUnavailable)
    assert result.reason is BatchUnavailable.Reason.WALLHAVEN_UNREACHABLE
    assert result.error and "set up to fail" in result.error
    assert result.error_at == harness.clock.now()


def test_a_recovered_refill_stops_claiming_wallhaven_is_unreachable(db_path: Path) -> None:
    """A transient failure must not stick. The last error is what the last call did, not what any did."""
    harness = make_harness(
        db_path, catalogue=catalogue_of(24), rate_limited_calls=1, retry_after=1.0, fill_pool=0
    )
    harness.fill_pool(1)
    assert isinstance(harness.core.get_next_batch(), BatchUnavailable)

    harness.clock.advance(1.0)
    harness.fill_pool(1)

    assert harness.core.refill_status().last_error is None
    assert isinstance(harness.core.get_next_batch(), Batch)


def test_a_failure_raised_by_something_other_than_the_client_still_does_not_escape(
    db_path: Path,
) -> None:
    """The protocol names one error, `RateLimited`, and everything else is "the call did not happen".

    Catching only `httpx2`'s exceptions would put the transport's spelling inside the Core service and
    would let one unexpected type kill the thread, which is the failure #15 is about.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(24), fill_pool=0)
    assert issubclass(WallhavenUnreachable, RuntimeError)

    harness.wallhaven.fail_from_call = 1
    harness.fill_pool(1)

    assert harness.core.refill_status().last_error


def test_the_refill_status_reports_the_pool_against_its_target(harness: Harness) -> None:
    """What the indicator on the **Batch** page is built from."""
    status = harness.core.refill_status()

    assert status.pool_size == 24
    assert status.target_size == 500
    assert not status.at_target
    assert status.last_run_at == harness.clock.now()
    assert status.running is False, "nothing started a thread here"
