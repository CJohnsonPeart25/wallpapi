"""The thumbnail downloader: every **Pool** member's thumbnail, fetched in the background (#44, ADR 0017).

Driven by hand through the Core service, the way `test_refill.py` drives the refill: `thumbnail_wait` says
how long the thread would wait and `thumbnail_step` does at most one fetch. No thread, no sleeping and no
network — the clock is the fake and so is the thumbnail host.

The thread itself — that it runs, that it runs alone, and that it stops — is `test_thumbnail_thread.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Harness, make_harness
from tests.fakes import WallhavenUnreachable, wallpaper
from wallpapi.core import THUMBNAIL_BACKOFF_SECONDS, THUMBNAIL_IDLE_RECHECK_SECONDS
from wallpapi.model import Wallpaper
from wallpapi.ratelimit import THUMBNAIL_GAP_SECONDS
from wallpapi.wallhaven import RateLimited, ThumbnailUnavailable

OUT_OF_ORDER = (wallpaper("cc0003"), wallpaper("aa0001"), wallpaper("bb0002"))
"""Admitted to the **Pool** in this order, and so not in `wallpaper_id` order."""


def _run(harness: Harness, steps: int) -> None:
    """Step the downloader as its thread would, letting the fake clock pass for every wait it asks for."""
    for _ in range(steps):
        harness.clock.advance(harness.core.thumbnail_wait())
        harness.core.thumbnail_step()


def _urls(wallpapers: tuple[Wallpaper, ...]) -> list[str]:
    return [w.thumbnail_url for w in wallpapers]


def test_every_pool_member_is_fetched_in_wallpaper_id_order(db_path: Path) -> None:
    """Acceptance criterion: sequential, in `wallpaper_id` order — not the order the refill admitted them."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)

    _run(harness, 3)

    by_id = tuple(sorted(OUT_OF_ORDER, key=lambda w: w.id))
    assert harness.wallhaven.thumbnail_fetches == _urls(by_id)
    assert sorted(p.name for p in harness.core.thumbnail_dir.iterdir()) == [f"{w.id}.jpg" for w in by_id]


def test_consecutive_fetches_are_at_least_the_gap_apart(db_path: Path) -> None:
    """Acceptance criterion: at least 0.25s between fetches, asked of the downloader rather than assumed."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)

    harness.core.thumbnail_step()

    assert harness.core.thumbnail_wait() == THUMBNAIL_GAP_SECONDS
    harness.clock.advance(0.1)
    assert harness.core.thumbnail_wait() == pytest.approx(0.15)  # pyright: ignore[reportUnknownMemberType]


def test_a_thumbnail_already_in_the_cache_is_never_fetched_again(db_path: Path) -> None:
    """Acceptance criterion. The tile route got there first, so the downloader leaves that one alone."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.get_thumbnail("aa0001")

    _run(harness, 3)

    assert harness.wallhaven.thumbnail_fetches.count(wallpaper("aa0001").thumbnail_url) == 1
    assert len(harness.wallhaven.thumbnail_fetches) == 3


def test_with_nothing_missing_the_downloader_looks_again_in_half_a_minute(db_path: Path) -> None:
    """#44: nothing missing, recheck every 30s. Not zero, which would spin on a directory listing."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _run(harness, 3)
    harness.clock.advance(THUMBNAIL_GAP_SECONDS)

    harness.core.thumbnail_step()

    assert harness.core.thumbnail_wait() == THUMBNAIL_IDLE_RECHECK_SECONDS == 30.0


def test_a_newcomer_to_the_pool_is_picked_up_on_the_next_look(db_path: Path) -> None:
    """The **Pool** turns over (ADR 0016), and every newcomer is work for the downloader."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER, page_size=2)
    _run(harness, 3)
    assert len(harness.wallhaven.thumbnail_fetches) == 2

    harness.fill_pool()
    _run(harness, 2)

    assert harness.wallhaven.thumbnail_fetches[-1] == wallpaper("bb0002").thumbnail_url


def test_a_refused_thumbnail_is_skipped_and_fetched_on_a_later_pass(db_path: Path) -> None:
    """Acceptance criterion: a failed fetch is retried on a later pass — not straight away, and not never.

    The host answered and said no to this one file. That says nothing about the rest, so the next member is
    fetched after only the ordinary gap, and the refused one waits for the next pass round the **Pool**.
    """
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    refused = wallpaper("aa0001").thumbnail_url
    harness.wallhaven.failing_thumbnails[refused] = ThumbnailUnavailable(404)

    harness.core.thumbnail_step()
    assert harness.core.thumbnail_wait() == THUMBNAIL_GAP_SECONDS
    _run(harness, 2)
    assert harness.wallhaven.thumbnail_fetches == _urls(tuple(sorted(OUT_OF_ORDER, key=lambda w: w.id)))
    # The pass is over. A file that keeps being refused is asked for once a pass, never four times a second.
    assert harness.core.thumbnail_wait() == THUMBNAIL_IDLE_RECHECK_SECONDS

    del harness.wallhaven.failing_thumbnails[refused]
    _run(harness, 1)

    assert harness.wallhaven.thumbnail_fetches[-1] == refused
    assert (harness.core.thumbnail_dir / "aa0001.jpg").exists()


@pytest.mark.parametrize(
    "failure",
    [RateLimited(None), RateLimited(5.0), WallhavenUnreachable("connection refused")],
    ids=["429", "429 asking for less", "connection error"],
)
def test_a_429_or_a_connection_error_backs_off_a_minute(db_path: Path, failure: Exception) -> None:
    """Acceptance criterion: a 429 or a connection error backs off 60s.

    Unlike a refusal these are about the host, not the file, so the next fetch would fail the same way. A
    `Retry-After` shorter than the minute does not shorten it: the minute is the downloader's own manners
    towards a host with no published limit.
    """
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.wallhaven.failing_thumbnails[wallpaper("aa0001").thumbnail_url] = failure

    harness.core.thumbnail_step()

    assert harness.core.thumbnail_wait() == THUMBNAIL_BACKOFF_SECONDS == 60.0


def test_a_429_asking_for_longer_than_a_minute_gets_it(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.wallhaven.failing_thumbnails[wallpaper("aa0001").thumbnail_url] = RateLimited(90.0)

    harness.core.thumbnail_step()

    assert harness.core.thumbnail_wait() == 90.0


def test_a_thumbnail_the_tile_route_fetched_mid_pass_is_not_fetched_again(db_path: Path) -> None:
    """#44: recheck, before each fetch, that the file still does not exist.

    The pass was listed before the tile rendered. Both writes would be atomic to the same path, so a race
    costs one request at most — but a pass that has been overtaken costs none.
    """
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.thumbnail_step()
    harness.core.get_thumbnail("bb0002")

    _run(harness, 2)

    assert harness.wallhaven.thumbnail_fetches.count(wallpaper("bb0002").thumbnail_url) == 1


def test_a_wallpaper_that_left_the_pool_mid_pass_is_not_fetched(db_path: Path) -> None:
    """Only **Pool** members are the downloader's work. One pruned after the pass was listed is skipped,
    rather than fetched for a submission's eviction to delete again."""
    catalogue = (wallpaper("aa0001"), wallpaper("bb0002", favourites=20), wallpaper("cc0003"))
    harness = make_harness(db_path, catalogue=catalogue)
    harness.core.thumbnail_step()
    harness.core.update_settings(min_favourites=50)

    _run(harness, 2)

    assert harness.wallhaven.thumbnail_fetches == _urls((catalogue[0], catalogue[2]))


def _fill_cache(harness: Harness, megabytes: int) -> Path:
    """A **Thumbnail cache** holding `megabytes` already — one file standing in for many."""
    harness.core.thumbnail_dir.mkdir(parents=True, exist_ok=True)
    filler = harness.core.thumbnail_dir / "zz9999.jpg"
    filler.write_bytes(b"\0" * (megabytes * 1024 * 1024))
    return filler


def test_a_cache_at_its_cap_fetches_nothing_and_looks_again_later(db_path: Path) -> None:
    """Acceptance criterion. Without it a cap below what the **Pool** needs would churn: the size-cap pass
    at submission deletes **Pool** thumbnails and the downloader fetches them straight back."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.update_settings(thumbnail_cache_max_mb=1)
    filler = _fill_cache(harness, 1)

    _run(harness, 3)

    assert harness.wallhaven.thumbnail_fetches == []
    assert harness.core.thumbnail_wait() == THUMBNAIL_IDLE_RECHECK_SECONDS

    filler.unlink()
    _run(harness, 1)

    assert harness.wallhaven.thumbnail_fetches == [wallpaper("aa0001").thumbnail_url]


def test_a_cache_under_its_cap_is_filled(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.update_settings(thumbnail_cache_max_mb=2)
    _fill_cache(harness, 1)

    _run(harness, 3)

    assert len(harness.wallhaven.thumbnail_fetches) == 3


def test_a_thumbnail_that_cannot_be_written_does_not_stop_the_downloader(db_path: Path) -> None:
    """`thumbnail_step` never raises, for `refill_step`'s reason: the thread has nothing to catch, and a
    dead downloader would leave the **Pool** unembedded with nothing saying why. A disk that refuses the
    write costs that one file, and it is asked for again on the next pass."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.core.thumbnail_dir.write_bytes(b"a file where the cache directory should be")

    _run(harness, 3)

    assert len(harness.wallhaven.thumbnail_fetches) == 3


# -- a thumbnail that never arrives (lead review of #56) ---------------------------------------------------

DEAD = wallpaper("aa0001")
"""The first **Pool** member in `wallpaper_id` order. Once the first pass has fetched the other two, it is
the only one missing, so each later pass is one step that asks for it."""


def _first_pass(harness: Harness) -> None:
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)
    _run(harness, len(OUT_OF_ORDER))


def test_one_refusal_is_retried_on_the_next_pass(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _first_pass(harness)

    _run(harness, 1)

    assert harness.wallhaven.thumbnail_fetches.count(DEAD.thumbnail_url) == 2


def test_two_refusals_in_a_row_are_never_asked_for_again_in_this_process(db_path: Path) -> None:
    """Otherwise a dead file is requested every thirty seconds for as long as wallpapi runs. Given up in
    memory only: a restart asks once more, as the embedder does with a file it could not read."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _first_pass(harness)
    _run(harness, 1)

    _run(harness, 10)

    assert harness.wallhaven.thumbnail_fetches.count(DEAD.thumbnail_url) == 2
    assert harness.core.thumbnail_wait() == THUMBNAIL_IDLE_RECHECK_SECONDS


def test_a_429_between_two_refusals_does_not_count_as_one(db_path: Path) -> None:
    """A 429 says the host is busy, not that the file is gone. Between two refusals it neither counts
    towards giving up nor wipes the first refusal out: it is asked for again, then given up on."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _first_pass(harness)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = RateLimited(None)
    _run(harness, 1)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)

    _run(harness, 10)

    assert harness.wallhaven.thumbnail_fetches.count(DEAD.thumbnail_url) == 3


def test_429s_and_failed_connections_never_add_up_to_giving_up(db_path: Path) -> None:
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = RateLimited(None)
    _run(harness, len(OUT_OF_ORDER))
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = WallhavenUnreachable("connection refused")
    _run(harness, 1)
    harness.wallhaven.failing_thumbnails[DEAD.thumbnail_url] = ThumbnailUnavailable(404)
    _run(harness, 1)

    _run(harness, 1)

    assert harness.wallhaven.thumbnail_fetches.count(DEAD.thumbnail_url) == 4


def test_the_notice_leaves_out_what_the_downloader_gave_up_on(db_path: Path) -> None:
    """Coverage is of what can be embedded. A **Pool** member whose thumbnail never arrives would hold it at
    499 of 500 for ever, and the line would never clear."""
    harness = make_harness(db_path, catalogue=OUT_OF_ORDER)
    _first_pass(harness)
    _run(harness, 1)

    harness.core.similarity_notice()

    assert harness.similarity.notice_pools == [("cc0003", "bb0002")]
