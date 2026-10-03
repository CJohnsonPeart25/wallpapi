"""The **Pool** and its refill: what gets in, when the refill runs, how fast, and what it does when Wallhaven
says no. Two strategies share one `refill_step`: random searches feed the **Unknowns**, and like: searches on
**Favourites** feed the **Bangers**.

Driven a step at a time through the Core service with the fake client and the fake clock: no thread, no
sleeping, so the whole 45-calls-a-minute budget is exercised in no real time. Searches are asserted against
the fake's record because the client is an injected seam and "searched with these parameters" has no other
observable.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.conftest import Harness, favourite, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import (
    ERROR_BACKOFF_SECONDS,
    IDLE_RECHECK_SECONDS,
    LIKE_PAGES_PER_FAVOURITE,
    Batch,
    BatchUnavailable,
    RefillStrategy,
)
from wallpapi.model import Verdict, Wallpaper
from wallpapi.ratelimit import CALLS_PER_MINUTE, WINDOW_SECONDS


def queries(harness: Harness) -> list[object]:
    """The `q` of every search made so far: `None` for a random one."""
    return [search["query"] for search in harness.wallhaven.searches]


def like_searches(harness: Harness) -> list[tuple[object, object]]:
    """The `(q, page)` of every like: search, in order."""
    return [(s["query"], s["page"]) for s in harness.wallhaven.searches if s["query"] is not None]


def walking(db_path: Path, **kwargs: object) -> Harness:
    """A harness whose refill would keep going: the target is far above anything the catalogue holds."""
    harness = make_harness(db_path, **kwargs)  # pyright: ignore[reportArgumentType]
    harness.core.update_settings(pool_target_size=1000)
    return harness


# -- what enters the Pool ----------------------------------------------------------------------------


def test_a_batch_is_drawn_from_the_pool_without_calling_wallhaven(harness: Harness) -> None:
    """The page load path makes no **API call**, so there is no network call on it to fail with a 500."""
    calls_before = len(harness.wallhaven.searches)

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len(batch.wallpapers) == 8
    assert len(harness.wallhaven.searches) == calls_before


def test_the_refill_searches_with_every_filter_and_the_fixed_masks(harness: Harness) -> None:
    """Purity SFW and every category on. No minimum **Favourites**, which Wallhaven has no parameter for,
    and no `q`, which only the like: strategy fills in."""
    assert harness.wallhaven.searches[0] == {
        "sorting": "random",
        "purity": "100",
        "categories": "111",
        "query": None,
        "page": 1,
        "seed": None,
        "atleast": "2560x1440",
        "ratios": "16x9,16x10,21x9",
    }


def test_the_search_follows_the_filters_when_they_change(harness: Harness) -> None:
    harness.core.update_settings(min_width=1920, min_height=1080, allowed_ratios="21x9")

    harness.core.refill_step()

    latest = harness.wallhaven.searches[-1]
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
    db_path: Path, catalogue: tuple[Wallpaper, ...], admitted: str
) -> None:
    """Checked locally even where the query asked for it: the API is trusted but not relied upon."""
    harness = make_harness(db_path, catalogue=catalogue)

    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == [admitted]


def test_the_same_wallpaper_met_twice_joins_the_pool_once(db_path: Path) -> None:
    repeated = tuple(wallpaper(f"dup{n:02d}") for n in range(10) for _ in range(3))
    harness = make_harness(db_path, catalogue=repeated, page_size=30)

    harness.core.refill_step()
    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert len({w.id for w in batch.wallpapers}) == 8


def test_changing_the_filters_prunes_undecided_pool_wallpapers_that_no_longer_pass(db_path: Path) -> None:
    harness = make_harness(
        db_path,
        catalogue=(wallpaper("modest", width=2560, height=1440), wallpaper("huge", width=3840, height=2160)),
    )

    harness.core.update_settings(min_width=3840, min_height=2160)
    batch = harness.core.get_next_batch()

    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == ["huge"]


def test_pruning_drops_decided_wallpapers_too_and_keeps_their_log_entries(db_path: Path) -> None:
    """A **Verdict** does not exempt a **Wallpaper** from a **Filter**, and only the **Pool** row goes.
    Decided from **History** before any **Batch** drew it, the one way left to have a decided member."""
    harness = make_harness(
        db_path,
        catalogue=(wallpaper("judged", width=2560, height=1440), wallpaper("huge", width=3840, height=2160)),
    )
    assert harness.core.edit_verdict("judged", Verdict.LIKE) is None
    entries_before = harness.core.list_history()

    harness.core.update_settings(min_width=3840, min_height=2160)

    assert harness.core.list_history() == entries_before, "the Decision log is append-only"
    assert harness.core.resolve_verdicts(["judged"])["judged"].verdict is Verdict.LIKE
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    assert [w.id for w in batch.wallpapers] == ["huge"]


def test_pruning_leaves_the_live_batch_and_its_drafts_alone(db_path: Path) -> None:
    """A **Batch** holds its own rows, so a **Pool** row going cannot take a tile or its mark with it."""
    harness = make_harness(db_path, catalogue=catalogue_of(10))
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    marked = live.wallpapers[0].id
    harness.core.set_draft_verdict(live.id, marked, Verdict.FAVOURITE)

    harness.core.update_settings(min_width=3840, min_height=2160, allowed_ratios="1x1")

    still_live = harness.core.get_next_batch()
    assert isinstance(still_live, Batch)
    assert still_live.id == live.id
    assert [w.id for w in still_live.wallpapers] == [w.id for w in live.wallpapers]
    assert still_live.drafts[marked] is Verdict.FAVOURITE


def test_a_settings_change_that_touches_no_filter_prunes_nothing(harness: Harness) -> None:
    before = harness.core.refill_status().pool_size

    harness.core.update_settings(batch_size=4, library_path=Path.home() / "elsewhere")

    assert harness.core.refill_status().pool_size == before


# -- when the refill runs ----------------------------------------------------------------------------


def test_the_refill_fills_the_pool_up_to_the_target_then_idles(db_path: Path) -> None:
    """Four a page and a target of ten: three calls, and the **Pool** overshoots to the page boundary. At
    target it waits long and cancellably, rather than spinning on a count."""
    harness = make_harness(db_path, catalogue=catalogue_of(40), page_size=4, fill_pool=0)
    harness.core.update_settings(pool_target_size=10)

    harness.fill_pool(4)

    assert len(harness.wallhaven.searches) == 3
    assert harness.core.refill_status().pool_size == 12
    assert harness.core.refill_status().at_target
    assert harness.core.refill_wait() == IDLE_RECHECK_SECONDS


def test_the_refill_runs_again_once_the_pool_drops_below_target(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=catalogue_of(24), page_size=4, fill_pool=0)
    harness.core.update_settings(pool_target_size=8, batch_size=4)
    harness.fill_pool(3)
    assert harness.core.refill_wait() == IDLE_RECHECK_SECONDS

    harness.core.update_settings(pool_target_size=20)

    assert harness.core.refill_wait() == 0.0
    calls_before = len(harness.wallhaven.searches)
    harness.fill_pool(1)
    assert len(harness.wallhaven.searches) == calls_before + 1


def test_the_refill_status_reports_the_pool_against_its_target(harness: Harness) -> None:
    status = harness.core.refill_status()

    assert status.pool_size == 24
    assert status.target_size == harness.core.get_settings().pool_target_size
    assert not status.at_target
    assert status.last_run_at == harness.clock.now()
    assert status.running is False, "nothing started a thread here"


@pytest.mark.parametrize(
    ("catalogue_size", "walk"),
    [
        pytest.param(40, [(1, None), (2, "seed-1"), (3, "seed-1")], id="carried across one walk"),
        pytest.param(4, [(1, None), (2, "seed-1"), (1, None)], id="an empty page starts a fresh one"),
    ],
)
def test_the_seed_is_carried_across_the_pages_of_one_walk_and_no_further(
    db_path: Path, catalogue_size: int, walk: list[tuple[int, str | None]]
) -> None:
    """Without the seed Wallhaven reshuffles on every page; reused across walks, it returns the same
    **Wallpapers**. An empty page, not `meta.last_page`, is the end of a walk."""
    harness = walking(
        db_path, catalogue=catalogue_of(catalogue_size), page_size=4, search_seed="seed-1", fill_pool=0
    )

    harness.fill_pool(3)

    assert [(s["page"], s["seed"]) for s in harness.wallhaven.searches] == walk


def test_reaching_the_target_ends_the_walk(db_path: Path) -> None:
    """Idling and then resuming is a new walk, on a new seed."""
    harness = make_harness(
        db_path, catalogue=catalogue_of(40), page_size=4, search_seed="seed-1", fill_pool=0
    )
    harness.core.update_settings(pool_target_size=6)
    harness.fill_pool(3)

    harness.core.update_settings(pool_target_size=1000)
    harness.fill_pool(1)

    assert harness.wallhaven.searches[-1]["page"] == 1
    assert harness.wallhaven.searches[-1]["seed"] is None


def test_forty_five_calls_a_minute_and_the_budget_frees_as_the_oldest_leave(db_path: Path) -> None:
    """The fake clock never moves on its own, so all 45 land in one instant and the 46th waits the whole
    window: a limiter that slept would have made this take 45 seconds. Then it waits only until the oldest
    ages out."""
    harness = walking(db_path, catalogue=catalogue_of(400), page_size=1, fill_pool=0)

    harness.fill_pool(CALLS_PER_MINUTE)

    assert len(harness.wallhaven.searches) == CALLS_PER_MINUTE
    assert harness.core.refill_wait() == WINDOW_SECONDS

    harness.clock.advance(WINDOW_SECONDS - 10)
    assert harness.core.refill_wait() == 10.0

    harness.clock.advance(10)
    assert harness.core.refill_wait() == 0.0
    harness.fill_pool(1)
    assert len(harness.wallhaven.searches) == CALLS_PER_MINUTE + 1


# -- when Wallhaven says no --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_after", "wait"),
    [
        pytest.param(17.0, 17.0, id="Retry-After honoured"),
        pytest.param(None, ERROR_BACKOFF_SECONDS, id="default"),
    ],
)
def test_a_429_backs_off_and_the_call_after_the_back_off_succeeds(
    db_path: Path, retry_after: float | None, wait: float
) -> None:
    """Wallhaven knows when it will answer again; without that, "immediately" would be the worst guess.
    A transient failure must not stick: the last error is what the last call did."""
    harness = make_harness(
        db_path, catalogue=catalogue_of(24), rate_limited_calls=1, retry_after=retry_after, fill_pool=0
    )

    harness.fill_pool(1)

    assert harness.core.refill_wait() == wait
    assert harness.core.refill_status().last_error
    assert isinstance(harness.core.get_next_batch(), BatchUnavailable)
    harness.clock.advance(wait)

    assert harness.core.refill_wait() == 0.0
    harness.fill_pool(1)
    assert harness.core.refill_status().pool_size == 24
    assert harness.core.refill_status().last_error is None
    assert isinstance(harness.core.get_next_batch(), Batch)


def test_a_transport_failure_is_recorded_and_the_empty_pool_says_wallhaven_is_unreachable(
    db_path: Path,
) -> None:
    """A failed **API call** never propagates out of the Core service, so the thread has nothing to catch.
    The **Batch unavailable** result carries the error and when, which is more help than "not working"."""
    harness = make_harness(db_path, catalogue=catalogue_of(24), fail_from_call=1, fill_pool=0)

    harness.fill_pool(1)

    status = harness.core.refill_status()
    assert status.last_error
    assert status.last_error_at == harness.clock.now()
    assert status.pool_size == 0
    assert harness.core.refill_wait() == ERROR_BACKOFF_SECONDS
    result = harness.core.get_next_batch()
    assert isinstance(result, BatchUnavailable)
    assert result.reason is BatchUnavailable.Reason.WALLHAVEN_UNREACHABLE
    assert result.error and "set up to fail" in result.error
    assert result.error_at == harness.clock.now()


def test_an_empty_pool_with_no_refill_yet_says_so(db_path: Path) -> None:
    harness = make_harness(db_path, fill_pool=0)

    result = harness.core.get_next_batch()

    assert isinstance(result, BatchUnavailable)
    assert result.reason is BatchUnavailable.Reason.POOL_EMPTY
    assert result.error is None


def test_a_failed_call_retries_the_same_page_rather_than_skipping_it(db_path: Path) -> None:
    """Advancing past it would thin the walk by a page every time the network hiccuped."""
    harness = walking(db_path, catalogue=catalogue_of(40), page_size=4, fail_from_call=2, fill_pool=0)

    harness.fill_pool(3)

    assert [s["page"] for s in harness.wallhaven.searches] == [1, 2, 2]


# -- the like: strategy ------------------------------------------------------------------------------


def liking(db_path: Path, like_results: dict[str, tuple[Wallpaper, ...]], **kwargs: object) -> Harness:
    return walking(db_path, catalogue=catalogue_of(2), like_results=like_results, fill_pool=1, **kwargs)


def test_like_searches_take_every_other_step_once_there_is_a_favourite_and_never_before(
    db_path: Path,
) -> None:
    """A fresh install has nothing to search like:, so taking turns before then would waste half the
    budget. After, strict alternation is what keeps either strategy from starving the other."""
    harness = liking(db_path, {"wp0000": catalogue_of(8, prefix="lk")})
    harness.fill_pool(2)
    assert queries(harness) == [None, None, None]
    assert harness.core.refill_status().last_strategy is RefillStrategy.RANDOM

    favourite(harness, "wp0000")
    harness.fill_pool(4)

    assert queries(harness)[3:] == ["like:wp0000", None, "like:wp0000", None]


def test_a_like_search_carries_the_favourite_and_the_same_filters(db_path: Path) -> None:
    """Sorted by relevance: the point is the most similar first, which a random sort would throw away."""
    harness = liking(db_path, {"wp0000": catalogue_of(8, prefix="lk")})
    harness.core.update_settings(min_width=1920, min_height=1080)
    favourite(harness, "wp0000")

    harness.fill_pool(1)

    assert harness.core.refill_status().last_strategy is RefillStrategy.LIKE
    assert harness.wallhaven.searches[-1] == {
        "sorting": "relevance",
        "query": "like:wp0000",
        "purity": "100",
        "categories": "111",
        "page": 1,
        "seed": None,
        "atleast": "1920x1080",
        "ratios": "16x9,16x10,21x9",
    }


def test_like_results_pass_the_same_filters_into_the_pool(db_path: Path) -> None:
    """The **Batch** that recorded the **Favourite** retired both random arrivals, so what the **Pool**
    holds is the like: walk's alone."""
    harness = liking(
        db_path, {"wp0000": (wallpaper("likeable"), wallpaper("toosmall", width=1280, height=720))}
    )
    favourite(harness, "wp0000")

    harness.fill_pool(1)

    assert like_searches(harness) == [("like:wp0000", 1)]
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    assert {w.id for w in live.wallpapers} == {"likeable"}


def test_a_like_result_already_in_the_pool_is_not_added_twice(db_path: Path) -> None:
    """Favourited from **History**, because a **Batch** would retire `wp0001` and the walk would then
    refuse it for having been decided, which is a different rule."""
    harness = liking(db_path, {"wp0000": (wallpaper("wp0001"), wallpaper("fresh"))})
    assert harness.core.edit_verdict("wp0000", Verdict.FAVOURITE) is None

    harness.fill_pool(1)

    assert like_searches(harness) == [("like:wp0000", 1)]
    assert harness.core.refill_status().pool_size == 3


def test_a_like_walk_stops_at_an_empty_page_and_moves_to_the_next_favourite(db_path: Path) -> None:
    """Which **Favourite** goes first is the seeded source's business, so this pins the shape of the walk."""
    harness = liking(
        db_path, {"wp0000": catalogue_of(2, prefix="la"), "wp0001": catalogue_of(2, prefix="lb")}, page_size=2
    )
    favourite(harness, "wp0000", "wp0001")

    harness.fill_pool(6)

    walked = like_searches(harness)
    first = walked[0][0]
    assert walked[0] == (first, 1)
    assert walked[1] == (first, 2), "the walk pages on while the page it got was full"
    assert walked[2][0] != first, "an empty page ends the walk and the next Favourite gets a turn"
    assert walked[2][1] == 1


def test_a_like_walk_stops_at_the_page_cap_and_moves_to_the_next_favourite(db_path: Path) -> None:
    """The tail of a like: result set is only weakly similar: six pages available, three taken."""
    harness = liking(
        db_path,
        {"wp0000": catalogue_of(12, prefix="la"), "wp0001": catalogue_of(12, prefix="lb")},
        page_size=2,
    )
    favourite(harness, "wp0000", "wp0001")

    harness.fill_pool(10)

    walked = like_searches(harness)
    first = walked[0][0]
    assert [page for subject, page in walked if subject == first] == [1, 2, 3]
    assert walked[LIKE_PAGES_PER_FAVOURITE][0] != first


def test_every_favourite_gets_a_turn_before_any_is_walked_twice(db_path: Path) -> None:
    """Empty like: results make every walk one page long, so the rotation is all that is on show."""
    harness = walking(
        db_path,
        catalogue=catalogue_of(3),
        like_results={"wp0000": (), "wp0001": (), "wp0002": ()},
        fill_pool=1,
    )
    favourite(harness, "wp0000", "wp0001", "wp0002")

    harness.fill_pool(12)

    walked = [subject for subject, _ in like_searches(harness)]
    assert set(walked[:3]) == {"like:wp0000", "like:wp0001", "like:wp0002"}
    assert set(walked[3:6]) == {"like:wp0000", "like:wp0001", "like:wp0002"}


def test_a_failed_like_search_backs_off_and_the_refill_carries_on_alternating(db_path: Path) -> None:
    """Recorded, backed off and never raised, and the failed page is retried as a random one is."""
    harness = liking(db_path, {"wp0000": catalogue_of(8, prefix="lk")})
    favourite(harness, "wp0000")

    harness.wallhaven.fail_from_call = 2
    harness.fill_pool(1)

    assert harness.core.refill_status().last_error
    assert harness.core.refill_status().last_strategy is RefillStrategy.LIKE
    assert harness.core.refill_wait() == ERROR_BACKOFF_SECONDS

    harness.wallhaven.fail_from_call = None
    harness.clock.advance(ERROR_BACKOFF_SECONDS)
    harness.fill_pool(2)

    assert queries(harness)[-2:] == [None, "like:wp0000"], "the back-off did not break the alternation"
    assert like_searches(harness)[-1] == ("like:wp0000", 1), "the failed page is retried, not skipped"


def test_the_combined_refill_still_stops_at_forty_five_calls_a_minute(db_path: Path) -> None:
    """For nothing, because a step is one call whichever strategy takes it. Driven the way the thread
    drives it, calling only when the wait is zero; the clock never moves."""
    harness = walking(
        db_path,
        catalogue=catalogue_of(400),
        page_size=1,
        like_results={"wp0000": catalogue_of(90, prefix="lk")},
        fill_pool=1,
    )
    favourite(harness, "wp0000")

    for _ in range(90):
        if harness.core.refill_wait() == 0.0:
            harness.core.refill_step()

    assert len(harness.wallhaven.searches) == CALLS_PER_MINUTE
    assert harness.core.refill_wait() == WINDOW_SECONDS
    assert {None, "like:wp0000"} <= set(queries(harness)), "both strategies ran inside the 45"


def test_a_favourite_that_is_replaced_drops_out_of_the_rotation(db_path: Path) -> None:
    """**Verdict resolution**, not the raw log, and mid-walk: the walk abandons the pages it had left."""
    harness = liking(
        db_path, {"wp0000": catalogue_of(8, prefix="la"), "wp0001": catalogue_of(8, prefix="lb")}, page_size=2
    )
    favourite(harness, "wp0000", "wp0001")
    harness.fill_pool(1)
    walking_subject = like_searches(harness)[0][0]
    demoted = str(walking_subject).removeprefix("like:")

    assert harness.core.edit_verdict(demoted, Verdict.LIKE) is None
    harness.fill_pool(6)

    assert walking_subject not in queries(harness)[-6:]
    assert like_searches(harness)[-1][0] != walking_subject
