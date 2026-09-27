"""The second refill strategy: "like:" searches on **Favourites**. Issue #13.

The random refill (#6) feeds the **Unknown** **Zone**; this one feeds the **Banger** **Zone** by asking
Wallhaven for the lookalikes of **Wallpapers** the user already loved. Both strategies are steps of the
same `refill_step`, which is why neither can starve the other and why the 45-calls-per-minute budget
covers the two of them for free: a step is still at most one **API call**.

Driven a step at a time through the Core service with the fake clock and a seeded random source. No
thread, no sleeping, no network.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import (
    ERROR_BACKOFF_SECONDS,
    LIKE_PAGES_PER_FAVOURITE,
    POOL_SOURCE_LIKE,
    POOL_SOURCE_RANDOM,
    Batch,
    RefillStrategy,
)
from wallpapi.model import Verdict
from wallpapi.ratelimit import CALLS_PER_MINUTE, WINDOW_SECONDS


def favourite(harness: Harness, *wallpaper_ids: str) -> None:
    """Record a **Favourite** the way the UI does: mint a **Batch**, mark it, submit it.

    Through the seam and never by writing a row. The **Wallpapers** have to be *in* the **Batch** to be
    judged, so every test here keeps the **Pool** small enough that one **Batch** shows all of it.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = {w.id for w in batch.wallpapers}
    assert set(wallpaper_ids) <= shown, f"{set(wallpaper_ids) - shown} is not in the batch to judge"
    for wallpaper_id in wallpaper_ids:
        harness.core.set_draft_verdict(batch.id, wallpaper_id, Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)


def judge(harness: Harness, wallpaper_id: str, verdict: Verdict) -> None:
    """Record any other **Verdict** the same way, so a **Favourite** can be replaced."""
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_draft_verdict(batch.id, wallpaper_id, verdict)
    harness.core.submit_batch(batch.id)


def queries(harness: Harness) -> list[object]:
    """The `q` of every search made so far — `None` for a random one."""
    return [search["query"] for search in harness.wallhaven.searches]


def like_searches(harness: Harness) -> list[tuple[object, object]]:
    """The `(q, page)` of every like: search, in order."""
    return [(s["query"], s["page"]) for s in harness.wallhaven.searches if s["query"] is not None]


def test_with_no_favourites_every_step_is_a_random_search(db_path: Path) -> None:
    """Acceptance criterion, first half: there is nothing to alternate with until something is loved.

    A fresh install has an empty **Decision log**, so a refill that insisted on taking turns would spend
    half its budget on searches it has no subject for.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(40), page_size=4, fill_pool=0)
    harness.core.update_settings(pool_target_size=1000)

    harness.fill_pool(4)

    assert queries(harness) == [None, None, None, None]
    assert {s["sorting"] for s in harness.wallhaven.searches} == {"random"}


def test_the_steps_alternate_once_there_is_a_favourite(db_path: Path) -> None:
    """Acceptance criterion: random refill continues alongside and neither strategy starves the other.

    Strict alternation is what "neither starves the other" is built out of here. It also keeps the combined
    refill inside the one limiter for nothing: a step is one **API call** whichever strategy takes it.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        like_results={"wp0000": catalogue_of(8, prefix="lk")},
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000")

    harness.fill_pool(4)

    assert queries(harness) == [None, "like:wp0000", None, "like:wp0000", None]


def test_a_like_search_carries_the_favourite_and_the_same_filters(db_path: Path) -> None:
    """Acceptance criterion: the results pass the same **Filters** — starting with asking for them.

    Asserted against the fake's recorded call rather than an outcome, for the reason `test_pool.py`'s
    equivalent gives: the Wallhaven client is a pre-agreed injected seam and "searched with these
    parameters" has no other observable. `sorting` is relevance rather than random — the point of a like:
    search is the most similar **Wallpapers** first, and a random sort over them would throw that away.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        like_results={"wp0000": catalogue_of(8, prefix="lk")},
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000, min_width=1920, min_height=1080)
    favourite(harness, "wp0000")

    harness.fill_pool(1)

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


def test_like_results_enter_the_pool_tagged_like_and_failing_ones_stay_out(db_path: Path) -> None:
    """Acceptance criterion: the results pass the same **Filters** before entering the **Pool**.

    The same local check the random strategy goes through, and for the same reason — the API is trusted
    but not relied upon. `source` is what tells the two strategies' contributions apart afterwards.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        like_results={"wp0000": (wallpaper("likeable"), wallpaper("toosmall", width=1280, height=720))},
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000")

    harness.fill_pool(1)

    assert harness.core.pool_sources() == {POOL_SOURCE_RANDOM: 2, POOL_SOURCE_LIKE: 1}
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    following = harness.core.submit_batch(live.id)
    assert isinstance(following, Batch)
    assert {w.id for w in following.wallpapers} == {"wp0000", "wp0001", "likeable"}


def test_a_wallpaper_already_in_the_pool_keeps_the_source_it_arrived_with(db_path: Path) -> None:
    """A like: search that returns something the random walk already found changes nothing.

    **Pool** membership is a row that says when a **Wallpaper** arrived and how; meeting it a second time
    is not a second arrival. `wp0001` is in both the catalogue and the like: results here.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        like_results={"wp0000": (wallpaper("wp0001"), wallpaper("fresh"))},
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000")

    harness.fill_pool(1)

    assert harness.core.pool_sources() == {POOL_SOURCE_RANDOM: 2, POOL_SOURCE_LIKE: 1}


def test_a_like_walk_stops_at_an_empty_page_and_moves_to_the_next_favourite(db_path: Path) -> None:
    """A like: result set is small: two pages in, there is nothing left to page through.

    Which **Favourite** is picked first is the seeded random source's business, so the assertion is about
    the shape of the walk rather than about a name.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        page_size=2,
        like_results={
            "wp0000": catalogue_of(2, prefix="la"),
            "wp0001": catalogue_of(2, prefix="lb"),
        },
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000", "wp0001")

    harness.fill_pool(6)

    walked = like_searches(harness)
    first = walked[0][0]
    assert walked[0] == (first, 1)
    assert walked[1] == (first, 2), "the walk pages on while the page it got was full"
    assert walked[2][0] != first, "an empty page ends the walk and the next Favourite gets a turn"
    assert walked[2][1] == 1


def test_a_like_walk_stops_at_the_page_cap_and_moves_to_the_next_favourite(db_path: Path) -> None:
    """The tail of a like: result set is only weakly similar, so the walk is capped rather than exhausted.

    Twelve results at two a page is six pages available and three taken.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        page_size=2,
        like_results={
            "wp0000": catalogue_of(12, prefix="la"),
            "wp0001": catalogue_of(12, prefix="lb"),
        },
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000", "wp0001")

    harness.fill_pool(10)

    walked = like_searches(harness)
    first = walked[0][0]
    assert [page for subject, page in walked if subject == first] == [1, 2, 3]
    assert walked[LIKE_PAGES_PER_FAVOURITE][0] != first


def test_every_favourite_gets_a_turn_before_any_is_walked_twice(db_path: Path) -> None:
    """Otherwise a seeded pick would be free to spend every like: step on the same **Wallpaper**.

    Each **Favourite**'s like: results are empty here, so each walk is one page long and the rotation is
    the only thing on show. When all three have had a turn the cycle restarts.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(3),
        like_results={"wp0000": (), "wp0001": (), "wp0002": ()},
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000", "wp0001", "wp0002")

    harness.fill_pool(12)

    walked = [subject for subject, _ in like_searches(harness)]
    assert set(walked[:3]) == {"like:wp0000", "like:wp0001", "like:wp0002"}
    assert set(walked[3:6]) == {"like:wp0000", "like:wp0001", "like:wp0002"}


def test_a_failed_like_search_backs_off_and_the_refill_carries_on_alternating(db_path: Path) -> None:
    """A like: failure is a refill failure: recorded, backed off, and never raised (#15).

    And the page it failed on is retried rather than skipped, exactly as a random page is — a page that
    was never fetched is a page whose **Wallpapers** were never seen.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        like_results={"wp0000": catalogue_of(8, prefix="lk")},
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
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
    """Acceptance criterion: the combined refill stays within 45 **API calls** a minute.

    For nothing, because a step is one call whichever strategy takes it — which is the point of making the
    strategies steps of one `refill_step` rather than two loops with a limiter each.

    Driven the way the thread drives it: the Core service says how long to wait and the caller does the
    waiting (invariant 11), so a test that called `refill_step` ninety times regardless would be testing
    something nothing in wallpapi does. The clock never moves, so all 45 land in one instant.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(400),
        page_size=1,
        like_results={"wp0000": catalogue_of(90, prefix="lk")},
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000")

    for _ in range(90):
        if harness.core.refill_wait() == 0.0:
            harness.core.refill_step()

    assert len(harness.wallhaven.searches) == CALLS_PER_MINUTE
    assert harness.core.refill_wait() == WINDOW_SECONDS
    assert harness.core.pool_sources().keys() == {POOL_SOURCE_RANDOM, POOL_SOURCE_LIKE}


def test_a_favourite_that_is_replaced_drops_out_of_the_rotation(db_path: Path) -> None:
    """**Verdict resolution**, not the raw log: a **Favourite** replaced by a **Like** is not one any more.

    Mid-walk, which is the case worth pinning: the walk abandons the **Wallpaper** it was on rather than
    finishing the pages it had left.
    """
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        page_size=2,
        like_results={
            "wp0000": catalogue_of(8, prefix="la"),
            "wp0001": catalogue_of(8, prefix="lb"),
        },
        fill_pool=1,
    )
    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000", "wp0001")
    harness.fill_pool(1)
    walking = like_searches(harness)[0][0]
    demoted = str(walking).removeprefix("like:")

    judge(harness, demoted, Verdict.LIKE)
    harness.fill_pool(6)

    assert walking not in queries(harness)[-6:]
    assert like_searches(harness)[-1][0] != walking


def test_the_refill_status_names_the_strategy_that_ran_last(db_path: Path) -> None:
    """What the indicator on the **Batch** page says the refill is spending its budget on."""
    harness = make_harness(
        db_path,
        catalogue=catalogue_of(2),
        like_results={"wp0000": catalogue_of(8, prefix="lk")},
        fill_pool=1,
    )
    assert harness.core.refill_status().last_strategy is RefillStrategy.RANDOM

    harness.core.update_settings(pool_target_size=1000)
    favourite(harness, "wp0000")
    harness.fill_pool(1)

    assert harness.core.refill_status().last_strategy is RefillStrategy.LIKE
