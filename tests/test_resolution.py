"""**Verdict resolution**: turning a **Wallpaper**'s **Decision log** entries into one value.

Issue #3's resolution criteria. Every test enters through the Core service, and the entries under test are
made the way the app makes them — by drafting and submitting — rather than by writing rows behind the seam.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch, ResolvedVerdict
from wallpapi.model import Verdict


def test_a_wallpaper_with_no_entries_resolves_to_nothing(harness: Harness) -> None:
    """The starting state of every **Wallpaper**: no **Verdict** at all, and a value of zero.

    Zero rather than absent as the value, because #9 sums these across a whole **Pool** and a `None` there
    would have to be special-cased at every call site.
    """
    resolved = harness.core.resolve_verdicts(["never-seen"])

    assert resolved["never-seen"].verdict is None
    assert resolved["never-seen"].value == 0


def test_ignores_stack_at_minus_ten_each(db_path: Path) -> None:
    """Acceptance criterion: each **Ignore** is -10, and they stack while no **Explicit Verdict** exists.

    The catalogue holds exactly 8, so both **Batches** show all 8 and every **Wallpaper** is **Ignored**
    twice. The resolved **Verdict** is the **Ignore** itself, not absence — "ignored twice" and "never seen"
    are different states and #7 renders them differently.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    harness.core.submit_batch(first.id)
    second = harness.core.get_next_batch()
    assert isinstance(second, Batch)
    harness.core.submit_batch(second.id)
    twice_ignored = first.wallpapers[0].id

    resolved = harness.core.resolve_verdicts([twice_ignored])

    assert resolved[twice_ignored].verdict is Verdict.IGNORE
    assert resolved[twice_ignored].value == -20


def submit_with(harness: Harness, marks: dict[str, Verdict]) -> Batch:
    """Draft `marks` against the live **Batch** and submit it, returning the **Batch** that was submitted.

    The catalogue these tests use holds exactly `batch_size` **Wallpapers**, so every **Batch** shows all of
    them and anything left out of `marks` is **Ignored**.
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


def test_an_explicit_verdict_disregards_ignores_before_and_after_it(db_path: Path) -> None:
    """Acceptance criterion: an **Explicit Verdict** disregards every **Ignore** on that **Wallpaper**.

    Three submissions: **Ignored**, then **Liked**, then **Ignored** again. The naive implementation sums
    everything and lands on +30; the one that lets the **Ignores** before it count lands on +40. Only
    +50 — the **Like** alone — is right. Scrolling past something you have already said you like is not
    evidence against it.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    subject = harness.core.get_next_batch()
    assert isinstance(subject, Batch)
    liked = subject.wallpapers[0].id

    submit_with(harness, {})
    submit_with(harness, {liked: Verdict.LIKE})
    submit_with(harness, {})

    assert harness.core.resolve_verdicts([liked])[liked] == ResolvedVerdict(verdict=Verdict.LIKE, value=50)


def test_a_later_explicit_verdict_replaces_an_earlier_one(db_path: Path) -> None:
    """Acceptance criterion: the latest **Explicit Verdict** wins outright, and the earlier one is not
    added to it. Changing your mind replaces the old judgement rather than averaging with it."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    subject = harness.core.get_next_batch()
    assert isinstance(subject, Batch)
    changed = subject.wallpapers[0].id

    submit_with(harness, {changed: Verdict.LIKE})
    submit_with(harness, {changed: Verdict.FAVOURITE})

    assert harness.core.resolve_verdicts([changed])[changed] == ResolvedVerdict(
        verdict=Verdict.FAVOURITE, value=100
    )


def test_two_explicit_verdicts_sharing_a_timestamp_resolve_to_the_later_one(db_path: Path) -> None:
    """Invariant 4: resolution orders by sequence, never by timestamp.

    The clock is frozen for both submissions, so the two **Explicit Verdicts** are indistinguishable by
    `recorded_at` — exactly the situation entries written in one transaction are in. An implementation that
    ordered by timestamp would be picking arbitrarily here and would pass or fail by luck of the row order.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    subject = harness.core.get_next_batch()
    assert isinstance(subject, Batch)
    changed = subject.wallpapers[0].id

    submit_with(harness, {changed: Verdict.FAVOURITE})
    submit_with(harness, {changed: Verdict.LIKE})

    history = [e for e in harness.core.list_history() if e.wallpaper_id == changed]
    assert len({e.recorded_at for e in history}) == 1, "the clock must not have moved"
    assert harness.core.resolve_verdicts([changed])[changed] == ResolvedVerdict(
        verdict=Verdict.LIKE, value=50
    )
