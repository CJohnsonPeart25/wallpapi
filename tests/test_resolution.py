"""**Verdict resolution**: turning a **Wallpaper**'s **Decision log** entries into one value.

Issue #3's resolution criteria, as #37 changed them: the latest entry decides, and a reshown **Wallpaper**
comes up marked with its latest **Verdict**. Every test enters through the Core service, and the entries
under test are made the way the app makes them — by drafting and submitting, and by editing from
**History** — rather than by writing rows behind the seam. The one exception is the legacy **Clearance**,
which nothing can make any more.

Since #38 a submitted **Wallpaper** leaves the **Pool** for good (ADR 0016), so a second entry for one is a
**History** edit, never a second **Batch**.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from tests.conftest import FIXED_NOW, Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch, ResolvedVerdict
from wallpapi.model import Clearance, Verdict


def test_a_wallpaper_with_no_entries_resolves_to_nothing(harness: Harness) -> None:
    """The starting state of every **Wallpaper**: no **Verdict** at all, and a value of zero.

    Zero rather than absent as the value, because #9 sums these across a whole **Pool** and a `None` there
    would have to be special-cased at every call site.
    """
    resolved = harness.core.resolve_verdicts(["never-seen"])

    assert resolved["never-seen"].verdict is None
    assert resolved["never-seen"].value == 0


def test_ignores_do_not_stack(db_path: Path) -> None:
    """An **Ignore** is -10 once, however many there are (#37, ADR 0015).

    **Ignored** by a **Batch** and then again from **History**. Passing a **Wallpaper** over twice is not
    twice the dislike of a **Dud** seen once. The resolved **Verdict** is the **Ignore** itself, not
    absence — "ignored" and "never seen" are different states.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    twice_ignored = submit_with(harness, {}).wallpapers[0].id
    assert harness.core.edit_verdict(twice_ignored, Verdict.IGNORE) is None

    resolved = harness.core.resolve_verdicts([twice_ignored])

    assert resolved[twice_ignored].verdict is Verdict.IGNORE
    assert resolved[twice_ignored].value == -10


def submit_with(harness: Harness, marks: Mapping[str, Verdict | None]) -> Batch:
    """Draft `marks` against the live **Batch** and submit it, returning the **Batch** that was submitted.

    The catalogue these tests use holds exactly `batch_size` **Wallpapers**, so the **Batch** shows all of
    them. A tile left out of `marks` keeps whatever the **Batch** was minted with — its latest **Explicit
    Verdict**, or nothing and so an **Ignore** — and a mark of `None` unmarks it, as clicking its mark
    again does on the page.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    for wallpaper_id, verdict in marks.items():
        harness.core.set_draft_verdict(batch.id, wallpaper_id, verdict)
    harness.core.submit_batch(batch.id)
    return batch


def test_each_explicit_verdict_resolves_to_its_own_value(db_path: Path) -> None:
    """Acceptance criterion: **Favourite** +100, **Like** +50, **Ban** -100.

    The numbers are the spec's, written out here as literals rather than recomputed from the enum, so this
    disagrees with the implementation if the implementation drifts.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    favourite, like, ban, *_ = [w.id for w in batch.wallpapers]
    for wallpaper_id, verdict in (
        (favourite, Verdict.FAVOURITE),
        (like, Verdict.LIKE),
        (ban, Verdict.BAN),
    ):
        harness.core.set_draft_verdict(batch.id, wallpaper_id, verdict)
    harness.core.submit_batch(batch.id)

    resolved = harness.core.resolve_verdicts([favourite, like, ban])

    assert resolved[favourite] == ResolvedVerdict(verdict=Verdict.FAVOURITE, value=100)
    assert resolved[like] == ResolvedVerdict(verdict=Verdict.LIKE, value=50)
    assert resolved[ban] == ResolvedVerdict(verdict=Verdict.BAN, value=-100)


def test_an_explicit_verdict_overturns_the_ignores_before_it(db_path: Path) -> None:
    """**Ignored** twice, then **Liked**: the latest entry decides, so +50 — the **Like** alone, with
    nothing subtracted for having been passed over first."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    liked = submit_with(harness, {}).wallpapers[0].id

    assert harness.core.edit_verdict(liked, Verdict.IGNORE) is None
    assert harness.core.edit_verdict(liked, Verdict.LIKE) is None

    assert harness.core.resolve_verdicts([liked])[liked] == ResolvedVerdict(verdict=Verdict.LIKE, value=50)


def decided_before_it_is_drawn(harness: Harness, verdicts: Mapping[str, Verdict]) -> Batch:
    """Give **Pool** members a **Verdict** from **History** before any **Batch** has shown them, then mint.

    Pre-marking is dormant since #38 — nothing reshows a decided **Wallpaper** (ADR 0016) — and kept for
    when re-evaluation does (#52). This is the one way the seam still reaches it: an edit appends to the
    **Decision log** and takes nothing out of the **Pool**, so the **Batch** minted next draws a
    **Wallpaper** that already has a decision.
    """
    for wallpaper_id, verdict in verdicts.items():
        assert harness.core.edit_verdict(wallpaper_id, verdict) is None
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    return batch


def test_a_reshown_wallpaper_comes_up_marked_with_its_latest_verdict(db_path: Path) -> None:
    """A **Batch** drawing a decided **Wallpaper** is minted with its **Explicit Verdict** already in the
    **Draft Batch**, so the tile shows it and the page knows what was decided last time (#37). An
    **Ignore** is not an **Explicit Verdict** and is not pre-filled; a **Ban** is never drawn at all."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    liked, favourite, banned, ignored = "wp0000", "wp0001", "wp0002", "wp0003"

    reshown = decided_before_it_is_drawn(
        harness,
        {liked: Verdict.LIKE, favourite: Verdict.FAVOURITE, banned: Verdict.BAN, ignored: Verdict.IGNORE},
    )

    assert reshown.drafts == {liked: Verdict.LIKE, favourite: Verdict.FAVOURITE}
    assert ignored in {w.id for w in reshown.wallpapers}
    assert banned not in {w.id for w in reshown.wallpapers}, "a Ban is never reshown"
    assert reshown == harness.core.get_next_batch(), "read back from storage, so a reload keeps it"


def test_a_verdict_given_from_history_is_what_the_next_batch_comes_up_marked_with(db_path: Path) -> None:
    """The latest decision wherever it was made: a **Favourite** overturned to a **Like** from **History**
    is pre-filled as the **Like**."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    subject = "wp0000"
    assert harness.core.edit_verdict(subject, Verdict.FAVOURITE) is None

    reshown = decided_before_it_is_drawn(harness, {subject: Verdict.LIKE})

    assert reshown.drafts == {subject: Verdict.LIKE}


def test_leaving_a_reshown_wallpaper_alone_records_its_verdict_again(db_path: Path) -> None:
    """Inaction keeps what was decided. Before #37 the untouched tile wrote an **Ignore** that resolution
    then had to disregard; now it writes the **Like** again, and the **Like** stands."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    liked = "wp0000"
    decided_before_it_is_drawn(harness, {liked: Verdict.LIKE})

    submit_with(harness, {})

    assert [e.entry for e in harness.core.list_history(wallpaper_id=liked)] == [Verdict.LIKE, Verdict.LIKE]
    assert harness.core.resolve_verdicts([liked])[liked] == ResolvedVerdict(verdict=Verdict.LIKE, value=50)


def test_unmarking_a_reshown_wallpaper_overturns_its_verdict(db_path: Path) -> None:
    """The heart of #37. **Liked**, then shown and unmarked: that **Ignore** is the latest entry and it
    decides. The **Like** no longer stands and the **Wallpaper** is worth -10."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    liked = "wp0000"
    decided_before_it_is_drawn(harness, {liked: Verdict.LIKE})

    submit_with(harness, {liked: None})

    assert harness.core.resolve_verdicts([liked])[liked] == ResolvedVerdict(verdict=Verdict.IGNORE, value=-10)


def test_select_none_unmarks_the_pre_filled_tiles_too(db_path: Path) -> None:
    """Select-none is a decision about every tile on the **Batch**, the pre-filled ones included."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    liked = "wp0000"
    batch = decided_before_it_is_drawn(harness, {liked: Verdict.LIKE})
    assert batch.drafts == {liked: Verdict.LIKE}

    assert harness.core.set_all_draft_verdicts(batch.id, None) is None
    harness.core.submit_batch(batch.id)

    assert harness.core.resolve_verdicts([liked])[liked].verdict is Verdict.IGNORE


def test_a_legacy_clearance_resolves_to_nothing_when_it_is_the_latest_entry(db_path: Path) -> None:
    """Nothing writes a **Clearance** since #37, but a database from before may hold one, and the log is
    append-only. When it is the latest entry the **Wallpaper** resolves to nothing, as if never seen;
    anything after it decides as usual.

    The one row in these tests written behind the seam, because no Core service operation can make it any
    more — which is the point.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    cleared, judged_again = "wp0000", "wp0001"
    submit_with(harness, {cleared: Verdict.FAVOURITE, judged_again: Verdict.BAN})
    connection = sqlite3.connect(db_path, isolation_level=None)
    try:
        connection.executemany(
            "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, NULL, ?, ?)",
            [
                (wallpaper_id, Clearance.CLEARED.value, FIXED_NOW.isoformat())
                for wallpaper_id in (cleared, judged_again)
            ],
        )
    finally:
        connection.close()
    assert harness.core.edit_verdict(judged_again, Verdict.LIKE) is None

    resolved = harness.core.resolve_verdicts([cleared, judged_again])

    assert resolved[cleared] == ResolvedVerdict(verdict=None, value=0)
    assert resolved[judged_again] == ResolvedVerdict(verdict=Verdict.LIKE, value=50)


def test_a_later_explicit_verdict_replaces_an_earlier_one(db_path: Path) -> None:
    """Acceptance criterion: the latest **Explicit Verdict** wins outright, and the earlier one is not
    added to it. Changing your mind replaces the old judgement rather than averaging with it."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    changed = "wp0000"
    submit_with(harness, {changed: Verdict.LIKE})

    assert harness.core.edit_verdict(changed, Verdict.FAVOURITE) is None

    assert harness.core.resolve_verdicts([changed])[changed] == ResolvedVerdict(
        verdict=Verdict.FAVOURITE, value=100
    )


def test_two_explicit_verdicts_sharing_a_timestamp_resolve_to_the_later_one(db_path: Path) -> None:
    """Invariant 4: resolution orders by sequence, never by timestamp.

    The clock is frozen for the submission and the **History** edit after it, so the two **Explicit
    Verdicts** are indistinguishable by `recorded_at` — exactly the situation entries written in one
    transaction, or two clicks inside one second, are in. An implementation that ordered by timestamp would
    be picking arbitrarily here and would pass or fail by luck of the row order.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    changed = "wp0000"
    submit_with(harness, {changed: Verdict.FAVOURITE})

    assert harness.core.edit_verdict(changed, Verdict.LIKE) is None

    history = [e for e in harness.core.list_history() if e.wallpaper_id == changed]
    assert len({e.recorded_at for e in history}) == 1, "the clock must not have moved"
    assert harness.core.resolve_verdicts([changed])[changed] == ResolvedVerdict(
        verdict=Verdict.LIKE, value=50
    )
