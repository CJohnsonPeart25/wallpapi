"""Verdict-aware **Thumbnail cache** eviction and its size cap. Invariant 8, deferred to #7 by #2.

Eviction is *not* "when it leaves the **Pool**". **History** renders a thumbnail for every past
**Verdict**, and a **Banned Wallpaper** leaves every **Zone** immediately and permanently while still
needing a picture on the page where the **Ban** can be undone.

Every test drives the cache the way the app does — `get_thumbnail` to fill it, `evict_thumbnails` or a
submission to empty it — and reads the result off `core.thumbnail_dir`, which is part of the seam.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import wallpaper
from wallpapi.core import Batch
from wallpapi.model import Verdict

BIG_THUMBNAIL = b"x" * (400 * 1024)
"""Four hundred kibibytes, so three of them are over a one-megabyte cap and two are under it.

The real thing is tens of kilobytes; a cap set in whole megabytes needs files large enough that a sensible
number of them crosses it without the test counting bytes at the assertion.
"""


def cached(harness: Harness) -> set[str]:
    """The **Wallpaper** IDs the **Thumbnail cache** currently holds a file for."""
    directory = harness.core.thumbnail_dir
    return {path.stem for path in directory.iterdir()} if directory.is_dir() else set()


def fill_cache(harness: Harness, *wallpaper_ids: str) -> None:
    """Fetch each thumbnail once, exactly as a page load does."""
    for wallpaper_id in wallpaper_ids:
        harness.core.get_thumbnail(wallpaper_id)


def test_an_undecided_wallpaper_that_left_the_pool_loses_its_thumbnail(db_path: Path) -> None:
    """The case eviction exists for, and with it the acceptance that **Verdicts** are what protect a file.

    Three **Wallpapers** are shown and judged: one **Banned**, one **Liked**, one left alone. A **Filter**
    change then prunes the undecided one out of the **Pool**, so nothing will ever show it again — and its
    thumbnail goes, while the two with **Explicit Verdicts** keep theirs because **History** renders them.
    """
    catalogue = (
        wallpaper("banned", width=3840, height=2160),
        wallpaper("liked", width=3840, height=2160),
        wallpaper("dropped", width=2560, height=1440),
    )
    harness = make_harness(db_path, catalogue=catalogue)
    harness.core.update_settings(batch_size=3)
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    fill_cache(harness, "banned", "liked", "dropped")
    harness.core.set_draft_verdict(first.id, "banned", Verdict.BAN)
    harness.core.set_draft_verdict(first.id, "liked", Verdict.LIKE)
    harness.core.submit_batch(first.id)
    assert cached(harness) == {"banned", "liked", "dropped"}, "a Pool member keeps its thumbnail"

    # Raises the minimum width past the undecided one, which drops it out of the **Pool**. The live
    # **Batch** still holds it, so it is not evictable until that **Batch** has been submitted.
    harness.core.update_settings(min_width=3000)
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    harness.core.submit_batch(live.id)

    assert cached(harness) == {"banned", "liked"}


def test_a_wallpaper_in_the_live_batch_keeps_its_thumbnail(db_path: Path) -> None:
    """A **Wallpaper** on screen is not evictable, even with no **Verdict** and no place in the **Pool**.

    A **Filter** change prunes the **Pool** but deliberately leaves the live **Batch** alone, so this is
    reachable: the tile is in front of the user and its thumbnail is what it is showing.
    """
    catalogue = (wallpaper("kept", width=3840, height=2160), wallpaper("shown", width=2560, height=1440))
    harness = make_harness(db_path, catalogue=catalogue)
    harness.core.update_settings(batch_size=2)
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    fill_cache(harness, "kept", "shown")
    harness.core.submit_batch(first.id)
    harness.core.update_settings(min_width=3000)
    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    assert "shown" in {w.id for w in live.wallpapers}, "the pruned Wallpaper must still be on screen"

    assert harness.core.evict_thumbnails().evicted == ()

    assert cached(harness) == {"kept", "shown"}


def test_eviction_does_nothing_when_every_thumbnail_is_wanted(db_path: Path) -> None:
    """The ordinary case, and the one that has to be cheap: everything is in the **Pool**, nothing goes."""
    harness = make_harness(db_path, catalogue=(wallpaper("one"), wallpaper("two")))
    fill_cache(harness, "one", "two")

    evicted = harness.core.evict_thumbnails()

    assert evicted.evicted == ()
    assert evicted.over_cap is False
    assert cached(harness) == {"one", "two"}


def test_a_file_belonging_to_no_wallpaper_is_swept_up(db_path: Path) -> None:
    """The leftovers of **Batches** abandoned before ADR 0002, which AGENTS.md deferred to this ticket.

    Their **Wallpapers** have no **Verdict** and no place in the **Pool**, and some have no `wallpapers`
    row at all. A file whose name matches nothing is exactly that case, and it is evicted for free: the
    name is the whole of the mapping from a file to a **Wallpaper**.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("one"),))
    fill_cache(harness, "one")
    (harness.core.thumbnail_dir / "abandoned.jpg").write_bytes(b"left over")

    assert harness.core.evict_thumbnails().evicted == ("abandoned",)

    assert cached(harness) == {"one"}


def test_the_size_cap_evicts_the_oldest_pool_thumbnails_first(db_path: Path) -> None:
    """The backstop. Three **Pool** members, no **Verdicts** anywhere, and a cap one of them over.

    Modification times are set by hand: wallpapi's clock is frozen in these tests and a real filesystem
    would give all three the same instant, which would make "oldest first" a coin toss rather than a rule.
    """
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
    """The cap does not get to break invariant 8's rule.

    Three **Favourites** well over a one-megabyte cap. **History** renders every one of them, so none may
    go — which means the cache stays over the cap, and `over_cap` says so rather than hiding it.
    """
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


def test_a_cleared_verdict_stops_protecting_a_thumbnail(db_path: Path) -> None:
    """A **Clearance** withdraws the **Explicit Verdict**, and with it the protection it gave.

    The **Wallpaper** still has a **History** row, and its thumbnail is fetched again the next time that
    row is rendered. That is the trade eviction makes everywhere: the cost of getting it wrong is one
    request to `th.wallhaven.cc`, never a broken page.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("judged", width=2560, height=1440),))
    harness.core.update_settings(batch_size=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    fill_cache(harness, "judged")
    harness.core.set_draft_verdict(batch.id, "judged", Verdict.BAN)
    harness.core.submit_batch(batch.id)
    harness.core.update_settings(min_width=3000)
    assert harness.core.evict_thumbnails().evicted == (), "a Ban protects the thumbnail"

    harness.core.clear_verdict("judged")

    assert harness.core.evict_thumbnails().evicted == ("judged",)
    assert cached(harness) == set()
    assert harness.core.get_thumbnail("judged") is not None, "and it is fetched again on demand"
