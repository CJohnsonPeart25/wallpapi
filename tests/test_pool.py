"""The **Pool** and its **Refill**, through `pool` alone: a real in-memory database, the fake Wallhaven client
and the fake clock. No Core service and no thread, so the whole 45-calls-a-minute budget is exercised in no
real time. Searches are asserted against the fake's record because the client is an injected seam and
"searched with these parameters" has no other observable.

**Favourites** are appended to the **Decision log** directly and the **Filters** changed through `settings`,
as their callers do; a **Batch** retiring what it showed is `test_batch.py`'s.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tests.conftest import FIXED_NOW
from tests.fakes import FakeClock, FakeWallhavenClient, catalogue_of, wallpaper
from wallpapi import decisions, pool, settings, storage
from wallpapi.model import Clearance, Verdict, Wallpaper
from wallpapi.pool import (
    CALLS_PER_MINUTE,
    ERROR_BACKOFF_SECONDS,
    IDLE_RECHECK_SECONDS,
    JOIN_TIMEOUT,
    LIKE_PAGES_PER_FAVOURITE,
    WINDOW_SECONDS,
    Refill,
    RefillStatus,
    RefillStrategy,
    wait_needed,
)
from wallpapi.rng import SeededRandom


@dataclass
class Rig:
    """One Refill over one database, with the fakes behind it."""

    connection: sqlite3.Connection
    refill: Refill
    wallhaven: FakeWallhavenClient
    clock: FakeClock

    def step(self, steps: int = 1) -> None:
        for _ in range(steps):
            self.refill.step()

    def configure(self, **fields: object) -> None:
        """Change the settings and prune the **Pool** in one transaction, as the settings page does."""
        with storage.write(self.connection) as write:
            updated = settings.update(write, **fields)
            assert isinstance(updated, settings.Settings), updated
            pool.prune(write, updated)

    def decide(self, verdict: Verdict, *wallpaper_ids: str) -> None:
        with storage.write(self.connection) as write:
            decisions.append(write, dict.fromkeys(wallpaper_ids, verdict), batch_id=None, at=self.clock.now())

    def favourite(self, *wallpaper_ids: str) -> None:
        self.decide(Verdict.FAVOURITE, *wallpaper_ids)

    def members(self) -> list[str]:
        return [w.id for w in pool.members(self.connection)]

    def queries(self) -> list[object]:
        """The `q` of every search made so far: `None` for a random one."""
        return [search["query"] for search in self.wallhaven.searches]

    def like_searches(self) -> list[tuple[object, object]]:
        """The `(q, page)` of every like: search, in order."""
        return [(s["query"], s["page"]) for s in self.wallhaven.searches if s["query"] is not None]


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    with closing(storage.connect(":memory:")) as connection:
        storage.migrate(connection)
        yield connection


def rig_over(
    connection: sqlite3.Connection,
    *,
    catalogue: Sequence[Wallpaper] | None = None,
    target: int | None = 1000,
    steps: int = 0,
    **client: object,
) -> Rig:
    """A Rig whose refill would keep going unless `target` says otherwise: 1000 is far above any catalogue."""
    wallhaven = FakeWallhavenClient(catalogue_of(24) if catalogue is None else catalogue, **client)  # pyright: ignore[reportArgumentType]
    clock = FakeClock(FIXED_NOW)
    rig = Rig(connection, Refill(lambda: connection, wallhaven, clock, SeededRandom(1)), wallhaven, clock)
    if target is not None:
        rig.configure(pool_target_size=target)
    rig.step(steps)
    return rig


@pytest.fixture
def rig(connection: sqlite3.Connection) -> Rig:
    return rig_over(connection, steps=1)


# -- what enters the Pool ----------------------------------------------------------------------------


def test_the_refill_searches_with_every_filter_and_the_fixed_masks(rig: Rig) -> None:
    """Purity SFW and every category on. No minimum **Favourites**, which Wallhaven has no parameter for,
    and no `q`, which only the like: strategy fills in."""
    assert rig.wallhaven.searches[0] == {
        "sorting": "random",
        "purity": "100",
        "categories": "111",
        "query": None,
        "page": 1,
        "seed": None,
        "atleast": "2560x1440",
        "ratios": "16x9,16x10,21x9",
    }


def test_the_search_follows_the_filters_when_they_change(rig: Rig) -> None:
    rig.configure(min_width=1920, min_height=1080, allowed_ratios="21x9")

    rig.step()

    latest = rig.wallhaven.searches[-1]
    assert latest["atleast"] == "1920x1080"
    assert latest["ratios"] == "21x9"


@pytest.mark.parametrize(
    ("catalogue", "admitted"),
    [
        pytest.param(
            (
                wallpaper("toosmall", width=1280, height=720),
                wallpaper("shortside", width=3840, height=1000),
                wallpaper("bigenough"),
            ),
            "bigenough",
            id="below the minimum resolution",
        ),
        # 4:3 at 2800x2100 passes the resolution Filter and fails the shape one. 3440x1440 is 2.39, not
        # 21x9's 2.33, and Wallhaven serves it under 21x9, so the local check must admit it.
        pytest.param(
            (
                wallpaper("fourbythree", width=2800, height=2100),
                wallpaper("ultrawide", width=3440, height=1440),
            ),
            "ultrawide",
            id="the wrong shape",
        ),
        pytest.param(
            (wallpaper("unloved", favourites=0), wallpaper("popular", favourites=10)),
            "popular",
            id="below the minimum favourites",
        ),
        pytest.param(
            (
                replace(wallpaper("sketchy"), purity="sketchy"),
                replace(wallpaper("explicit"), purity="nsfw"),
                wallpaper("wholesome"),
            ),
            "wholesome",
            id="not sfw",
        ),
    ],
)
def test_a_wallpaper_failing_a_filter_never_enters_the_pool(
    connection: sqlite3.Connection, catalogue: tuple[Wallpaper, ...], admitted: str
) -> None:
    """Checked locally even where the query asked for it: the API is trusted but not relied upon."""
    rig = rig_over(connection, catalogue=catalogue, steps=1)

    assert rig.members() == [admitted]


def test_the_same_wallpaper_met_twice_joins_the_pool_once(connection: sqlite3.Connection) -> None:
    repeated = tuple(wallpaper(f"dup{n:02d}") for n in range(10) for _ in range(3))

    rig = rig_over(connection, catalogue=repeated, page_size=30, steps=1)

    assert rig.members() == [f"dup{n:02d}" for n in range(10)]


def test_a_decided_wallpaper_met_again_is_not_admitted(connection: sqlite3.Connection) -> None:
    """ADR 0016: decided once. Any entry at all refuses it, a legacy **Clearance** included, because that
    resolves to nothing yet was a decision; the `wallpapers` row is still refreshed."""
    rig = rig_over(connection, catalogue=catalogue_of(4), target=None)
    with storage.write(connection) as write:
        write.executemany(
            "INSERT INTO wallpapers (id, width, height, ratio, category, purity, favourites, colours, "
            "thumbnail_url, full_url, page_url) VALUES (?, 1, 1, '', '', 'sfw', 0, '', '', '', '')",
            [("wp0000",), ("wp0001",)],
        )
    rig.decide(Verdict.IGNORE, "wp0000")
    with storage.write(connection) as write:
        write.execute(
            "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, NULL, ?, ?)",
            ("wp0001", Clearance.CLEARED.value, FIXED_NOW.isoformat()),
        )

    rig.step()

    assert rig.members() == ["wp0002", "wp0003"]
    refreshed = connection.execute("SELECT width FROM wallpapers WHERE id = 'wp0000'").fetchone()
    assert refreshed["width"] == 3840


def test_admission_stamps_the_first_arrival_as_utc_from_the_clock(connection: sqlite3.Connection) -> None:
    """Invariant 5: `fetched_at` is an ISO 8601 UTC string from the injected clock, never the machine's, and a
    **Wallpaper** met again keeps the time it first arrived."""
    rig = rig_over(connection, catalogue=catalogue_of(1), steps=1)
    rig.clock.advance(3600)
    rig.step()

    (stored,) = [row["fetched_at"] for row in connection.execute("SELECT fetched_at FROM pool")]

    assert stored == FIXED_NOW.isoformat()
    assert dt.datetime.fromisoformat(stored).tzinfo == dt.UTC


def test_changing_the_filters_prunes_the_members_that_no_longer_pass(connection: sqlite3.Connection) -> None:
    """A **Verdict** does not exempt a member from a **Filter**, and only the **Pool** row goes: the
    **Decision log** keeps its entry."""
    rig = rig_over(
        connection,
        catalogue=(
            wallpaper("modest", width=2560, height=1440),
            wallpaper("judged", width=2560, height=1440),
            wallpaper("huge", width=3840, height=2160),
        ),
        steps=1,
    )
    rig.decide(Verdict.LIKE, "judged")

    rig.configure(min_width=3840, min_height=2160)

    assert rig.members() == ["huge"]
    assert decisions.resolve(connection, ["judged"])["judged"].verdict is Verdict.LIKE


def test_retiring_drops_only_the_named_members_and_keeps_their_rows(connection: sqlite3.Connection) -> None:
    """What a submitted **Batch** showed leaves the **Pool** (ADR 0016); the `wallpapers` row stays, since
    **History** and the **Library** still show it, and a name not in the **Pool** is no error."""
    rig = rig_over(connection, catalogue=catalogue_of(4), steps=1)

    with storage.write(connection) as write:
        pool.retire(write, ["wp0001", "wp0003", "never-admitted"])

    assert rig.members() == ["wp0000", "wp0002"]
    assert connection.execute("SELECT COUNT(*) FROM wallpapers").fetchone()[0] == 4


def test_a_settings_change_that_touches_no_filter_prunes_nothing(rig: Rig) -> None:
    before = rig.members()

    rig.configure(batch_size=4, library_path=Path.home() / "elsewhere")

    assert rig.members() == before


# -- when the refill runs ----------------------------------------------------------------------------


def test_the_refill_fills_the_pool_up_to_the_target_then_idles(connection: sqlite3.Connection) -> None:
    """Four a page and a target of ten: three calls, and the **Pool** overshoots to the page boundary. At
    target it waits long and cancellably, rather than spinning on a count."""
    rig = rig_over(connection, catalogue=catalogue_of(40), page_size=4, target=10)

    rig.step(4)

    assert len(rig.wallhaven.searches) == 3
    assert rig.refill.status().pool_size == 12
    assert rig.refill.status().at_target
    assert rig.refill.wait() == IDLE_RECHECK_SECONDS


def test_the_refill_runs_again_once_the_pool_drops_below_target(connection: sqlite3.Connection) -> None:
    rig = rig_over(connection, catalogue=catalogue_of(24), page_size=4, target=8, steps=3)
    assert rig.refill.wait() == IDLE_RECHECK_SECONDS

    rig.configure(pool_target_size=20)

    assert rig.refill.wait() == 0.0
    calls_before = len(rig.wallhaven.searches)
    rig.step()
    assert len(rig.wallhaven.searches) == calls_before + 1


def test_the_status_reports_the_pool_against_its_target_and_whether_the_loop_is_running(rig: Rig) -> None:
    status = rig.refill.status()

    assert status == RefillStatus(
        pool_size=24,
        target_size=1000,
        running=False,
        last_run_at=FIXED_NOW,
        last_error=None,
        last_error_at=None,
        last_strategy=RefillStrategy.RANDOM,
    )
    assert not status.at_target
    with rig.refill.running():
        assert rig.refill.status().running
    assert not rig.refill.status().running


@pytest.mark.parametrize(
    ("catalogue_size", "walk"),
    [
        pytest.param(40, [(1, None), (2, "seed-1"), (3, "seed-1")], id="carried across one walk"),
        pytest.param(4, [(1, None), (2, "seed-1"), (1, None)], id="an empty page starts a fresh one"),
    ],
)
def test_the_seed_is_carried_across_the_pages_of_one_walk_and_no_further(
    connection: sqlite3.Connection, catalogue_size: int, walk: list[tuple[int, str | None]]
) -> None:
    """Without the seed Wallhaven reshuffles on every page; reused across walks, it returns the same
    **Wallpapers**. An empty page, not `meta.last_page`, is the end of a walk."""
    rig = rig_over(connection, catalogue=catalogue_of(catalogue_size), page_size=4, seed="seed-1")

    rig.step(3)

    assert [(s["page"], s["seed"]) for s in rig.wallhaven.searches] == walk


def test_reaching_the_target_ends_the_walk(connection: sqlite3.Connection) -> None:
    """Idling and then resuming is a new walk, on a new seed."""
    rig = rig_over(connection, catalogue=catalogue_of(40), page_size=4, seed="seed-1", target=6, steps=3)

    rig.configure(pool_target_size=1000)
    rig.step()

    assert rig.wallhaven.searches[-1]["page"] == 1
    assert rig.wallhaven.searches[-1]["seed"] is None


def test_forty_five_calls_a_minute_and_the_budget_frees_as_the_oldest_leave(
    connection: sqlite3.Connection,
) -> None:
    """The fake clock never moves on its own, so all 45 land in one instant and the 46th waits the whole
    window: a limiter that slept would have made this take 45 seconds. Then it waits only until the oldest
    ages out."""
    rig = rig_over(connection, catalogue=catalogue_of(400), page_size=1)

    rig.step(CALLS_PER_MINUTE)

    assert len(rig.wallhaven.searches) == CALLS_PER_MINUTE
    assert rig.refill.wait() == WINDOW_SECONDS

    rig.clock.advance(WINDOW_SECONDS - 10)
    assert rig.refill.wait() == 10.0

    rig.clock.advance(10)
    assert rig.refill.wait() == 0.0
    rig.step()
    assert len(rig.wallhaven.searches) == CALLS_PER_MINUTE + 1


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
def test_the_minute_window(calls: Sequence[float], now: float, wait: float) -> None:
    """Pure "how long must I wait" over monotonic timestamps (ADR 0005); the caller waits. Monotonic, because
    a wall clock jumping back over a daylight-saving change would hand out free calls."""
    assert wait_needed(calls, now=now) == wait


# -- when Wallhaven says no --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_after", "wait"),
    [
        pytest.param(17.0, 17.0, id="Retry-After honoured"),
        pytest.param(None, ERROR_BACKOFF_SECONDS, id="default"),
    ],
)
def test_a_429_backs_off_and_the_call_after_the_back_off_succeeds(
    connection: sqlite3.Connection, retry_after: float | None, wait: float
) -> None:
    """Wallhaven knows when it will answer again; without that, "immediately" would be the worst guess.
    A transient failure must not stick: the last error is what the last call did."""
    rig = rig_over(connection, rate_limited_calls=1, retry_after=retry_after, steps=1)

    assert rig.refill.wait() == wait
    assert rig.refill.status().last_error
    rig.clock.advance(wait)

    assert rig.refill.wait() == 0.0
    rig.step()
    assert rig.refill.status().pool_size == 24
    assert rig.refill.status().last_error is None
    assert rig.refill.status().last_error_at is None


def test_a_transport_failure_is_recorded_with_when_and_never_raised(connection: sqlite3.Connection) -> None:
    """A failed **API call** never propagates out of `step`, so the thread has nothing to catch."""
    rig = rig_over(connection, fail_from_call=1)
    rig.clock.advance(5)

    rig.step()

    status = rig.refill.status()
    assert status.last_error and "set up to fail" in status.last_error
    assert status.last_error_at == FIXED_NOW + dt.timedelta(seconds=5)
    assert status.pool_size == 0
    assert rig.refill.wait() == ERROR_BACKOFF_SECONDS


def test_a_failed_call_retries_the_same_page_rather_than_skipping_it(connection: sqlite3.Connection) -> None:
    """Advancing past it would thin the walk by a page every time the network hiccuped."""
    rig = rig_over(connection, catalogue=catalogue_of(40), page_size=4, fail_from_call=2)

    rig.step(3)

    assert [s["page"] for s in rig.wallhaven.searches] == [1, 2, 2]


def test_the_status_is_one_snapshot_while_a_step_is_mid_search(tmp_path: Path) -> None:
    """Read from a request thread while the refill thread is held inside its **API call**: the lock is not
    held across the call, so `status` answers, and it answers with this step's run and strategy beside the
    last call's error and its time, never one without the other. Then the step lands and clears both."""
    connections = storage.ThreadConnections(tmp_path / "wallpapi.db")
    storage.migrate(connections.get())
    wallhaven = FakeWallhavenClient(catalogue_of(24), fail_from_call=1)
    clock = FakeClock(FIXED_NOW)
    refill = Refill(connections.get, wallhaven, clock, SeededRandom(1))
    refill.step()
    failed = refill.status()
    assert failed.last_error is not None
    clock.advance(ERROR_BACKOFF_SECONDS)
    wallhaven.fail_from_call = None
    wallhaven.searched.clear()
    release = threading.Event()
    wallhaven.hold_searches = release
    stepping = threading.Thread(target=refill.step)

    seen: list[RefillStatus] = []
    # Read on a third thread, so a lock held across the call fails the join rather than hanging the test.
    reading = threading.Thread(target=lambda: seen.append(refill.status()))

    stepping.start()
    try:
        assert wallhaven.searched.wait(JOIN_TIMEOUT), "the step should be inside its search"
        reading.start()
        reading.join(JOIN_TIMEOUT)
        assert not reading.is_alive(), "status waited on the step's API call"
    finally:
        release.set()
        stepping.join(JOIN_TIMEOUT)

    (during,) = seen
    assert during == replace(failed, last_run_at=FIXED_NOW + dt.timedelta(seconds=ERROR_BACKOFF_SECONDS))
    after = refill.status()
    assert (after.last_error, after.last_error_at, after.pool_size) == (None, None, 24)


# -- the like: strategy ------------------------------------------------------------------------------


def liking(
    connection: sqlite3.Connection, like_results: dict[str, tuple[Wallpaper, ...]], *, page_size: int = 24
) -> Rig:
    """Two random arrivals, `wp0000` and `wp0001`, to favourite."""
    return rig_over(
        connection, catalogue=catalogue_of(2), like_results=like_results, page_size=page_size, steps=1
    )


def test_like_searches_take_every_other_step_once_there_is_a_favourite_and_never_before(
    connection: sqlite3.Connection,
) -> None:
    """A fresh install has nothing to search like:, so taking turns before then would waste half the
    budget. After, strict alternation is what keeps either strategy from starving the other."""
    rig = liking(connection, {"wp0000": catalogue_of(8, prefix="lk")})
    rig.step(2)
    assert rig.queries() == [None, None, None]
    assert rig.refill.status().last_strategy is RefillStrategy.RANDOM

    rig.favourite("wp0000")
    rig.step(4)

    assert rig.queries()[3:] == ["like:wp0000", None, "like:wp0000", None]


def test_a_like_search_carries_the_favourite_and_the_same_filters(connection: sqlite3.Connection) -> None:
    """Sorted by relevance: the point is the most similar first, which a random sort would throw away."""
    rig = liking(connection, {"wp0000": catalogue_of(8, prefix="lk")})
    rig.configure(min_width=1920, min_height=1080)
    rig.favourite("wp0000")

    rig.step()

    assert rig.refill.status().last_strategy is RefillStrategy.LIKE
    assert rig.wallhaven.searches[-1] == {
        "sorting": "relevance",
        "query": "like:wp0000",
        "purity": "100",
        "categories": "111",
        "page": 1,
        "seed": None,
        "atleast": "1920x1080",
        "ratios": "16x9,16x10,21x9",
    }


def test_like_results_pass_the_same_filters_into_the_pool(connection: sqlite3.Connection) -> None:
    rig = liking(
        connection, {"wp0000": (wallpaper("likeable"), wallpaper("toosmall", width=1280, height=720))}
    )
    rig.favourite("wp0000")

    rig.step()

    assert rig.like_searches() == [("like:wp0000", 1)]
    assert rig.members() == ["wp0000", "wp0001", "likeable"]


def test_a_like_result_already_in_the_pool_is_not_added_twice(connection: sqlite3.Connection) -> None:
    rig = liking(connection, {"wp0000": (wallpaper("wp0001"), wallpaper("fresh"))})
    rig.favourite("wp0000")

    rig.step()

    assert rig.like_searches() == [("like:wp0000", 1)]
    assert rig.members() == ["wp0000", "wp0001", "fresh"]


def test_a_like_walk_stops_at_an_empty_page_and_moves_to_the_next_favourite(
    connection: sqlite3.Connection,
) -> None:
    """Which **Favourite** goes first is the seeded source's business, so this pins the shape of the walk."""
    rig = liking(
        connection,
        {"wp0000": catalogue_of(2, prefix="la"), "wp0001": catalogue_of(2, prefix="lb")},
        page_size=2,
    )
    rig.favourite("wp0000", "wp0001")

    rig.step(6)

    walked = rig.like_searches()
    first = walked[0][0]
    assert walked[0] == (first, 1)
    assert walked[1] == (first, 2), "the walk pages on while the page it got was full"
    assert walked[2][0] != first, "an empty page ends the walk and the next Favourite gets a turn"
    assert walked[2][1] == 1


def test_a_like_walk_stops_at_the_page_cap_and_moves_to_the_next_favourite(
    connection: sqlite3.Connection,
) -> None:
    """The tail of a like: result set is only weakly similar: six pages available, three taken."""
    rig = liking(
        connection,
        {"wp0000": catalogue_of(12, prefix="la"), "wp0001": catalogue_of(12, prefix="lb")},
        page_size=2,
    )
    rig.favourite("wp0000", "wp0001")

    rig.step(10)

    walked = rig.like_searches()
    first = walked[0][0]
    assert [page for subject, page in walked if subject == first] == [1, 2, 3]
    assert walked[LIKE_PAGES_PER_FAVOURITE][0] != first


def test_every_favourite_gets_a_turn_before_any_is_walked_twice(connection: sqlite3.Connection) -> None:
    """Empty like: results make every walk one page long, so the rotation is all that is on show."""
    rig = rig_over(
        connection,
        catalogue=catalogue_of(3),
        like_results={"wp0000": (), "wp0001": (), "wp0002": ()},
        steps=1,
    )
    rig.favourite("wp0000", "wp0001", "wp0002")

    rig.step(12)

    walked = [subject for subject, _ in rig.like_searches()]
    assert set(walked[:3]) == {"like:wp0000", "like:wp0001", "like:wp0002"}
    assert set(walked[3:6]) == {"like:wp0000", "like:wp0001", "like:wp0002"}


def test_a_failed_like_search_backs_off_and_the_refill_carries_on_alternating(
    connection: sqlite3.Connection,
) -> None:
    """Recorded, backed off and never raised, and the failed page is retried as a random one is."""
    rig = liking(connection, {"wp0000": catalogue_of(8, prefix="lk")})
    rig.favourite("wp0000")

    rig.wallhaven.fail_from_call = 2
    rig.step()

    assert rig.refill.status().last_error
    assert rig.refill.status().last_strategy is RefillStrategy.LIKE
    assert rig.refill.wait() == ERROR_BACKOFF_SECONDS

    rig.wallhaven.fail_from_call = None
    rig.clock.advance(ERROR_BACKOFF_SECONDS)
    rig.step(2)

    assert rig.queries()[-2:] == [None, "like:wp0000"], "the back-off did not break the alternation"
    assert rig.like_searches()[-1] == ("like:wp0000", 1), "the failed page is retried, not skipped"


def test_the_combined_refill_still_stops_at_forty_five_calls_a_minute(connection: sqlite3.Connection) -> None:
    """For nothing, because a step is one call whichever strategy takes it. Driven the way the thread
    drives it, calling only when the wait is zero; the clock never moves."""
    rig = rig_over(
        connection,
        catalogue=catalogue_of(400),
        page_size=1,
        like_results={"wp0000": catalogue_of(90, prefix="lk")},
        steps=1,
    )
    rig.favourite("wp0000")

    for _ in range(90):
        if rig.refill.wait() == 0.0:
            rig.step()

    assert len(rig.wallhaven.searches) == CALLS_PER_MINUTE
    assert rig.refill.wait() == WINDOW_SECONDS
    assert {None, "like:wp0000"} <= set(rig.queries()), "both strategies ran inside the 45"


def test_a_favourite_that_is_replaced_drops_out_of_the_rotation(connection: sqlite3.Connection) -> None:
    """**Verdict resolution**, not the raw log, and mid-walk: the walk abandons the pages it had left."""
    rig = liking(
        connection,
        {"wp0000": catalogue_of(8, prefix="la"), "wp0001": catalogue_of(8, prefix="lb")},
        page_size=2,
    )
    rig.favourite("wp0000", "wp0001")
    rig.step()
    walking_subject = rig.like_searches()[0][0]
    demoted = str(walking_subject).removeprefix("like:")

    rig.decide(Verdict.LIKE, demoted)
    rig.step(6)

    assert walking_subject not in rig.queries()[-6:]
    assert rig.like_searches()[-1][0] != walking_subject
