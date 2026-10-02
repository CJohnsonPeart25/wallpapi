"""The **History** listing as a Core service operation: one row per **Wallpaper**, filtered and paged.

**History** is a view over the **Decision log**, not a second store, so every row here is produced by
judging **Wallpapers** the way the app does and nothing is written to make a listing exist.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import HISTORY_PAGE_SIZE, Batch
from wallpapi.model import Verdict


def judge(harness: Harness, **marks: Verdict) -> None:
    """Give each named **Wallpaper** a **Verdict** from **History**, in the order given."""
    for wallpaper_id, verdict in marks.items():
        assert harness.core.edit_verdict(wallpaper_id, verdict) is None


def test_a_wallpaper_with_no_entries_has_no_row(db_path: Path) -> None:
    """**History** lists what has been judged. An untouched **Pool** produces no rows at all."""
    harness = make_harness(db_path, catalogue=catalogue_of(4))

    listing = harness.core.list_history_rows()

    assert listing.rows == ()
    assert listing.total == 0
    assert listing.pages == 1


def test_rows_carry_the_wallpaper_its_resolved_verdict_and_its_latest_timestamp(db_path: Path) -> None:
    """The acceptance criterion: thumbnail, **Verdict** and timestamp, one row per **Wallpaper**.

    The thumbnail is the **Wallpaper** itself — the page serves it from `/thumb/{id}` — and the
    **Verdict** is the *resolved* one rather than the latest entry, which is the whole difference between
    **History** and a print-out of the log.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("only"),))
    harness.core.update_settings(batch_size=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_draft_verdict(batch.id, "only", Verdict.LIKE)
    harness.core.submit_batch(batch.id)

    row = harness.core.list_history_rows().rows[0]

    assert row.wallpaper.id == "only"
    assert row.wallpaper.page_url == "https://wallhaven.cc/w/only"
    assert row.resolved.verdict is Verdict.LIKE
    assert row.latest_at == harness.clock.now()


def test_one_row_per_wallpaper_however_many_entries_it_has(db_path: Path) -> None:
    """Four entries for one **Wallpaper** are four facts and one row.

    A listing of entries would show the same image four times and offer four places to change a
    **Verdict** that has one current value.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("only"),))
    harness.core.update_settings(batch_size=1)
    judge(harness, only=Verdict.LIKE)
    judge(harness, only=Verdict.IGNORE)
    judge(harness, only=Verdict.BAN)

    listing = harness.core.list_history_rows()

    assert listing.total == 1
    assert listing.rows[0].resolved.verdict is Verdict.BAN
    assert len(harness.core.list_history(wallpaper_id="only")) == 3


def test_rows_are_ordered_by_newest_activity(db_path: Path) -> None:
    """Newest first, ordered by sequence and not by timestamp (invariant 4).

    The clock never moves in these tests, so every entry shares a `recorded_at` and an implementation
    ordering by it would return the three rows in whatever order the query planner chose.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    judge(harness, wp0000=Verdict.LIKE, wp0001=Verdict.BAN, wp0002=Verdict.FAVOURITE)
    judge(harness, wp0001=Verdict.LIKE)

    listing = harness.core.list_history_rows()

    assert [row.wallpaper.id for row in listing.rows] == ["wp0001", "wp0002", "wp0000"]
    assert len({row.latest_at for row in listing.rows}) == 1, "the clock must not have moved"


def test_the_filter_narrows_to_one_resolved_verdict(db_path: Path) -> None:
    """Filtered by what a **Wallpaper** resolves to *now*, not by what it was ever given.

    The **Banned** one was **Liked** first, so a filter over raw entries would list it under both.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    judge(harness, wp0000=Verdict.LIKE, wp0001=Verdict.LIKE, wp0002=Verdict.FAVOURITE)
    judge(harness, wp0001=Verdict.BAN)

    liked = harness.core.list_history_rows(verdict=Verdict.LIKE)

    assert [row.wallpaper.id for row in liked.rows] == ["wp0000"]
    assert liked.total == 1
    assert [r.wallpaper.id for r in harness.core.list_history_rows(verdict=Verdict.BAN).rows] == ["wp0001"]


def test_ignores_can_be_filtered_for_and_filtered_out(db_path: Path) -> None:
    """**Ignore** is a resolved **Verdict** like any other, and it is the bulk of every **History**.

    Filtering *to* it is how the user finds what they scrolled past; filtering to anything else is how
    they get the thousands of them off the page.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    harness.core.update_settings(batch_size=3)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_draft_verdict(batch.id, "wp0000", Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)

    ignored = harness.core.list_history_rows(verdict=Verdict.IGNORE)

    assert {row.wallpaper.id for row in ignored.rows} == {"wp0001", "wp0002"}
    assert harness.core.list_history_rows(verdict=Verdict.FAVOURITE).total == 1


def test_a_verdict_withdrawn_from_history_is_listed_under_ignore(db_path: Path) -> None:
    """An **Explicit Verdict** withdrawn with an **Ignore** (#37) resolves to that **Ignore**, so the row
    moves from its old filter to the **Ignore** one rather than to no filter at all."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    judge(harness, wp0000=Verdict.FAVOURITE)
    judge(harness, wp0000=Verdict.IGNORE)

    assert [r.wallpaper.id for r in harness.core.list_history_rows(verdict=Verdict.IGNORE).rows] == ["wp0000"]
    assert harness.core.list_history_rows(verdict=Verdict.FAVOURITE).total == 0


def test_pages_are_a_hundred_rows_newest_first(db_path: Path) -> None:
    """Paging, so a **History** of thousands of **Ignores** does not render as one page.

    A hundred and one judged **Wallpapers**: a full first page, the oldest single row on the second, and
    a page number past the end clamped back rather than refused — a stale link should land on the last
    page rather than on an error.
    """
    count = HISTORY_PAGE_SIZE + 1
    harness = make_harness(db_path, catalogue=catalogue_of(count), page_size=count)
    judge(harness, **{f"wp{n:04d}": Verdict.LIKE for n in range(count)})

    first = harness.core.list_history_rows()
    second = harness.core.list_history_rows(page=2)

    assert first.total == count
    assert first.pages == 2
    assert len(first.rows) == HISTORY_PAGE_SIZE
    assert first.rows[0].wallpaper.id == f"wp{count - 1:04d}"
    assert [row.wallpaper.id for row in second.rows] == ["wp0000"]
    assert harness.core.list_history_rows(page=99).page == 2
    assert harness.core.list_history_rows(page=0).page == 1


def test_a_single_row_reads_back_what_an_edit_left(db_path: Path) -> None:
    """What the page swaps in after an edit: the same row the listing would give, read from the log."""
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    judge(harness, wp0000=Verdict.LIKE)

    assert harness.core.get_history_row("wp0001") is None, "an unjudged Wallpaper has no row"
    row = harness.core.get_history_row("wp0000")

    assert row is not None
    assert row.resolved.verdict is Verdict.LIKE
    assert harness.core.get_history_row("never-seen") is None
