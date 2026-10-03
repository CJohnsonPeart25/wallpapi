"""The **Thumbnail cache**: verdict-aware eviction with a size cap (ADR 0009), and the background
downloader that fills it with every **Pool** member (ADR 0017).

The downloader is driven by hand, as the refill is: `thumbnail_wait` says how long the thread would wait and
`thumbnail_step` does at most one fetch. The cache is read off `core.thumbnail_dir`, which is part of the
seam.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.conftest import Harness, make_harness
from tests.fakes import WallhavenUnreachable, wallpaper
from wallpapi.core import THUMBNAIL_BACKOFF_SECONDS, THUMBNAIL_IDLE_RECHECK_SECONDS, Batch
from wallpapi.model import Verdict, Wallpaper
from wallpapi.ratelimit import THUMBNAIL_GAP_SECONDS
from wallpapi.wallhaven import RateLimited, ThumbnailUnavailable

BIG_THUMBNAIL = b"x" * (400 * 1024)
"""Three of these are over a one-megabyte cap and two are under it."""


def cached(harness: Harness) -> set[str]:
    directory = harness.core.thumbnail_dir
    return {path.stem for path in directory.iterdir()} if directory.is_dir() else set()


def fill_cache(harness: Harness, *wallpaper_ids: str) -> None:
    """Fetch each thumbnail once, exactly as a page load does."""
    for wallpaper_id in wallpaper_ids:
        harness.core.get_thumbnail(wallpaper_id)


# -- eviction ----------------------------------------------------------------------------------------


def test_an_undecided_wallpaper_that_left_the_pool_loses_its_thumbnail(db_path: Path) -> None:
    """Eviction is not "when it leaves the **Pool**": an **Explicit Verdict** keeps its thumbnail because
    **History** renders it. A retired **Ignore** loses its file at the submission that recorded it
    (ADR 0016); a **Pool** member keeps its file until a **Filter** change prunes it."""
    catalogue = (
        wallpaper("banned", width=3840, height=2160),
        wallpaper("liked", width=3840, height=2160),
        wallpaper("ignored", width=3840, height=2160),
        wallpaper("dropped", width=2560, height=1440),
    )
    harness = make_harness(db_path, catalogue=catalogue, page_size=3)
    harness.core.update_settings(batch_size=3)
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    fill_cache(harness, "banned", "liked", "ignored")
    harness.core.set_draft_verdict(first.id, "banned", Verdict.BAN)
    harness.core.set_draft_verdict(first.id, "liked", Verdict.LIKE)
    harness.core.submit_batch(first.id)
    assert cached(harness) == {"banned", "liked"}, "a retired Ignore keeps nothing"

    harness.fill_pool(1)
    fill_cache(harness, "dropped")
    assert harness.core.evict_thumbnails().evicted == (), "a Pool member keeps its thumbnail"
    harness.core.update_settings(min_width=3000)

    assert harness.core.evict_thumbnails().evicted == ("dropped",)
    assert cached(harness) == {"banned", "liked"}


def test_a_wallpaper_in_the_live_batch_keeps_its_thumbnail(db_path: Path) -> None:
    """A **Filter** change prunes the **Pool** and leaves the live **Batch** alone, so a tile on screen can
    have no **Verdict** and no place in the **Pool**."""
    catalogue = (wallpaper("kept", width=3840, height=2160), wallpaper("shown", width=2560, height=1440))
    harness = make_harness(db_path, catalogue=catalogue)
    harness.core.update_settings(batch_size=2)
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    fill_cache(harness, "kept", "shown")
    harness.core.update_settings(min_width=3000)
    assert "shown" in {w.id for w in live.wallpapers}, "the pruned Wallpaper must still be on screen"

    assert harness.core.evict_thumbnails().evicted == ()

    assert cached(harness) == {"kept", "shown"}


def test_a_file_belonging_to_no_wallpaper_is_swept_up(db_path: Path) -> None:
    """The file name is the whole mapping from a file to a **Wallpaper**, so one matching nothing goes."""
    harness = make_harness(db_path, catalogue=(wallpaper("one"),))
    fill_cache(harness, "one")
    (harness.core.thumbnail_dir / "abandoned.jpg").write_bytes(b"left over")

    assert harness.core.evict_thumbnails().evicted == ("abandoned",)

    assert cached(harness) == {"one"}


def test_the_size_cap_evicts_the_oldest_pool_thumbnails_first(db_path: Path) -> None:
    """Modification times are set by hand, or a real filesystem gives all three the same instant."""
    harness = make_harness(
        db_path,
        catalogue=(wallpaper("oldest"), wallpaper("middle"), wallpaper("newest")),
        thumbnail_bytes=BIG_THUMBNAIL,
    )
    harness.core.update_settings(thumbnail_cache_max_mb=1)
    fill_cache(harness, "oldest", "middle", "newest")
    for age, wallpaper_id in enumerate(("oldest", "middle", "newest")):
        os.utime(harness.core.thumbnail_dir / f"{wallpaper_id}.jpg", (1_000 + age, 1_000 + age))

    evicted = harness.core.evict_thumbnails()

    assert evicted.evicted == ("oldest",)
    assert evicted.over_cap is False
    assert cached(harness) == {"middle", "newest"}


def test_the_size_cap_never_evicts_an_explicit_verdict(db_path: Path) -> None:
    """**History** renders every **Favourite**, so the cache stays over the cap and `over_cap` says so."""
    harness = make_harness(
        db_path,
        catalogue=(wallpaper("one"), wallpaper("two"), wallpaper("three")),
        thumbnail_bytes=BIG_THUMBNAIL,
    )
    harness.core.update_settings(batch_size=3, thumbnail_cache_max_mb=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    fill_cache(harness, "one", "two", "three")
    harness.core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)

    evicted = harness.core.evict_thumbnails()

    assert evicted.evicted == ()
    assert evicted.over_cap is True
    assert cached(harness) == {"one", "two", "three"}


def test_a_withdrawn_verdict_stops_protecting_a_thumbnail(db_path: Path) -> None:
    """An **Ignore** from **History** overturns the **Explicit Verdict** and its protection. Getting it
    wrong costs one request, never a broken page: the row fetches it again on demand."""
    harness = make_harness(db_path, catalogue=(wallpaper("judged", width=2560, height=1440),))
    harness.core.update_settings(batch_size=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    fill_cache(harness, "judged")
    harness.core.set_draft_verdict(batch.id, "judged", Verdict.BAN)
    harness.core.submit_batch(batch.id)
    harness.core.update_settings(min_width=3000)
    assert harness.core.evict_thumbnails().evicted == (), "a Ban protects the thumbnail"

    harness.core.edit_verdict("judged", Verdict.IGNORE)

    assert harness.core.evict_thumbnails().evicted == ("judged",)
    assert cached(harness) == set()
    assert harness.core.get_thumbnail("judged") is not None, "and it is fetched again on demand"


# -- the background downloader -----------------------------------------------------------------------

OUT_OF_ORDER = (wallpaper("cc0003"), wallpaper("aa0001"), wallpaper("bb0002"))
"""Admitted to the **Pool** in this order, and so not in `wallpaper_id` order."""

DEAD = wallpaper("aa0001")
"""First in `wallpaper_id` order: once a pass has fetched the other two, each later pass is one step."""


def _run(harness: Harness, steps: int) -> None:
    """Step the downloader as its thread would, letting the fake clock pass for every wait it asks for."""
    for _ in range(steps):
        harness.clock.advance(harness.core.thumbnail_wait())
        harness.core.thumbnail_step()


def _urls(wallpapers: tuple[Wallpaper, ...]) -> list[str]:
    return [w.thumbnail_url for w in wallpapers]


def _fetches_of(harness: Harness, wallpaper_id: str) -> int:
    return harness.wallhaven.thumbnail_fetches.count(wallpaper(wallpaper_id).thumbnail_url)


def test_every_pool_member_is_fetched_in_wallpaper_id_order(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)

    _run(harness, 3)

    by_id = tuple(sorted(OUT_OF_ORDER, key=lambda w: w.id))
    assert harness.wallhaven.thumbnail_fetches == _urls(by_id)
    assert sorted(p.name for p in harness.core.thumbnail_dir.iterdir()) == [f"{w.id}.jpg" for w in by_id]


def test_consecutive_fetches_are_at_least_the_gap_apart(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)

    harness.core.thumbnail_step()

    assert harness.core.thumbnail_wait() == THUMBNAIL_GAP_SECONDS
    harness.clock.advance(0.1)
    assert harness.core.thumbnail_wait() == pytest.approx(THUMBNAIL_GAP_SECONDS - 0.1)  # pyright: ignore[reportUnknownMemberType]


def test_a_thumbnail_already_in_the_cache_is_never_fetched_again(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.get_thumbnail("aa0001")

    _run(harness, 3)

    assert _fetches_of(harness, "aa0001") == 1
    assert len(harness.wallhaven.thumbnail_fetches) == 3


def test_a_thumbnail_the_tile_route_fetched_mid_pass_is_not_fetched_again(db_path: Path) -> None:
    """The pass was listed before the tile rendered, so each fetch rechecks that the file is still
    missing."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.thumbnail_step()
    harness.core.get_thumbnail("bb0002")

    _run(harness, 2)

    assert _fetches_of(harness, "bb0002") == 1


def test_a_wallpaper_that_left_the_pool_mid_pass_is_not_fetched(db_path: Path) -> None:
    """Otherwise it is fetched only for a submission's eviction to delete again."""
    catalogue = (wallpaper("aa0001"), wallpaper("bb0002", favourites=20), wallpaper("cc0003"))
    harness = make_harness(db_path, catalogue=catalogue)
    harness.core.thumbnail_step()
    harness.core.update_settings(min_favourites=50)

    _run(harness, 2)

    assert harness.wallhaven.thumbnail_fetches == _urls((catalogue[0], catalogue[2]))


def test_with_nothing_missing_the_downloader_looks_again_later(db_path: Path) -> None:
    """Not zero, which would spin on a directory listing."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _run(harness, 3)
    harness.clock.advance(THUMBNAIL_GAP_SECONDS)

    harness.core.thumbnail_step()

    assert harness.core.thumbnail_wait() == THUMBNAIL_IDLE_RECHECK_SECONDS


def test_a_newcomer_to_the_pool_is_picked_up_on_the_next_look(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER, page_size=2)
    _run(harness, 3)
    assert len(harness.wallhaven.thumbnail_fetches) == 2

    harness.fill_pool()
    _run(harness, 2)

    assert harness.wallhaven.thumbnail_fetches[-1] == wallpaper("bb0002").thumbnail_url


def test_a_refused_thumbnail_is_skipped_and_fetched_on_a_later_pass(db_path: Path) -> None:
    """A refusal is about one file, so the next member follows after the ordinary gap; the refused one is
    asked for once a pass, never four times a second."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    refused = DEAD.thumbnail_url
    harness.wallhaven.failing_thumbnails[refused] = ThumbnailUnavailable(404)

    harness.core.thumbnail_step()
    assert harness.core.thumbnail_wait() == THUMBNAIL_GAP_SECONDS
    _run(harness, 2)
    assert harness.wallhaven.thumbnail_fetches == _urls(tuple(sorted(OUT_OF_ORDER, key=lambda w: w.id)))
    assert harness.core.thumbnail_wait() == THUMBNAIL_IDLE_RECHECK_SECONDS

    del harness.wallhaven.failing_thumbnails[refused]
    _run(harness, 1)

    assert harness.wallhaven.thumbnail_fetches[-1] == refused
    assert (harness.core.thumbnail_dir / "aa0001.jpg").exists()


@pytest.mark.parametrize(
    ("failure", "wait"),
    [
        pytest.param(RateLimited(None), THUMBNAIL_BACKOFF_SECONDS, id="429"),
        pytest.param(RateLimited(5.0), THUMBNAIL_BACKOFF_SECONDS, id="429 asking for less"),
        pytest.param(RateLimited(90.0), 90.0, id="429 asking for more"),
        pytest.param(
            WallhavenUnreachable("connection refused"), THUMBNAIL_BACKOFF_SECONDS, id="connection error"
        ),
    ],
)
def test_a_429_or_a_connection_error_backs_off(db_path: Path, failure: Exception, wait: float) -> None:
    """About the host, not the file, so the next fetch would fail the same way. A shorter `Retry-After`
    does not shorten the back-off: that is the downloader's own manners towards a host with no published
    limit."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = failure

    harness.core.thumbnail_step()

    assert harness.core.thumbnail_wait() == wait


def test_a_cache_at_its_cap_fetches_nothing_until_it_is_under_it(db_path: Path) -> None:
    """Without it a cap below what the **Pool** needs would churn against the size-cap pass."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.update_settings(thumbnail_cache_max_mb=1)
    harness.core.thumbnail_dir.mkdir(parents=True, exist_ok=True)
    filler = harness.core.thumbnail_dir / "zz9999.jpg"
    filler.write_bytes(b"\0" * (1024 * 1024))

    _run(harness, 3)

    assert harness.wallhaven.thumbnail_fetches == []
    assert harness.core.thumbnail_wait() == THUMBNAIL_IDLE_RECHECK_SECONDS

    filler.unlink()
    _run(harness, 1)

    assert harness.wallhaven.thumbnail_fetches == [wallpaper("aa0001").thumbnail_url]


def test_a_thumbnail_that_cannot_be_written_does_not_stop_the_downloader(db_path: Path) -> None:
    """`thumbnail_step` never raises: a dead downloader would leave the **Pool** unembedded with nothing
    saying why."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.thumbnail_dir.write_bytes(b"a file where the cache directory should be")

    _run(harness, 3)

    assert len(harness.wallhaven.thumbnail_fetches) == 3


def _first_pass_refusing_the_dead_one(harness: Harness) -> None:
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)
    _run(harness, len(OUT_OF_ORDER))


def test_two_refusals_in_a_row_are_never_asked_for_again_in_this_process(db_path: Path) -> None:
    """One refusal is retried on the next pass; two in a row are given up on, or a dead file is asked for
    every thirty seconds for as long as wallpapi runs. In memory only: a restart asks once more."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _first_pass_refusing_the_dead_one(harness)

    _run(harness, 1)
    assert _fetches_of(harness, DEAD.id) == 2

    _run(harness, 10)

    assert _fetches_of(harness, DEAD.id) == 2
    assert harness.core.thumbnail_wait() == THUMBNAIL_IDLE_RECHECK_SECONDS


def test_a_429_between_two_refusals_does_not_count_as_one(db_path: Path) -> None:
    """A 429 says the host is busy, not that the file is gone: it neither counts towards giving up nor
    wipes the first refusal out."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _first_pass_refusing_the_dead_one(harness)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = RateLimited(None)
    _run(harness, 1)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)

    _run(harness, 10)

    assert _fetches_of(harness, DEAD.id) == 3


def test_429s_and_failed_connections_never_add_up_to_giving_up(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = RateLimited(None)
    _run(harness, len(OUT_OF_ORDER))
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = WallhavenUnreachable("connection refused")
    _run(harness, 1)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)
    _run(harness, 1)

    _run(harness, 1)

    assert _fetches_of(harness, DEAD.id) == 4


def test_the_notice_leaves_out_what_the_downloader_gave_up_on(db_path: Path) -> None:
    """Coverage is of what can be embedded, or a dead thumbnail would hold the line on the page for ever."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _first_pass_refusing_the_dead_one(harness)
    _run(harness, 1)

    harness.core.similarity_notice()

    assert harness.similarity.notice_pools == [("cc0003", "bb0002")]
