"""**Clearance** and **History** edits: issue #7's **Core service** half.

A **Clearance** is an entry in its own right, not a fifth **Verdict**. What it does is withdraw an
**Explicit Verdict**, after which the **Wallpaper**'s **Ignores** stack again — the ones from before it as
well as the ones from after.

Every test enters through the Core service, and every entry under test is made the way the app makes it:
by drafting and submitting, or by `edit_verdict` and `clear_verdict`.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import Batch, BatchUnavailable, HistoryRefused, ResolvedVerdict
from wallpapi.model import Clearance, Verdict


def submit_with(harness: Harness, marks: dict[str, Verdict]) -> Batch:
    """Draft `marks` against the live **Batch** and submit it, returning the **Batch** submitted.

    The catalogues here hold exactly `batch_size` **Wallpapers**, so every **Batch** shows all of them and
    anything left out of `marks` is **Ignored**.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    for wallpaper_id, verdict in marks.items():
        harness.core.set_draft_verdict(batch.id, wallpaper_id, verdict)
    harness.core.submit_batch(batch.id)
    return batch


def test_clearing_lets_the_ignores_before_and_after_stack_again(db_path: Path) -> None:
    """The acceptance criterion at the heart of the ticket.

    **Ignored**, then **Liked**, then **Ignored** again, then **Cleared**. While the **Like** stood, both
    **Ignores** were disregarded and the **Wallpaper** was worth +50. Withdrawing it does not leave the
    **Wallpaper** blank and does not leave only the later **Ignore** counting: nothing is disregarding
    either of them any more, so it is worth -20.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    subject = first.wallpapers[0].id

    submit_with(harness, {})
    submit_with(harness, {subject: Verdict.LIKE})
    submit_with(harness, {})
    assert harness.core.resolve_verdicts([subject])[subject].value == 50, "the Like must be standing"

    assert harness.core.clear_verdict(subject) is None

    assert harness.core.resolve_verdicts([subject])[subject] == ResolvedVerdict(
        verdict=Verdict.IGNORE, value=-20
    )


def test_a_verdict_after_a_clearance_wins(db_path: Path) -> None:
    """A **Clearance** is not a floor the **Wallpaper** is stuck on. Judging it again resolves to the new
    **Verdict** outright, and the **Ignores** go back to being disregarded."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    subject = first.wallpapers[0].id
    submit_with(harness, {})
    submit_with(harness, {subject: Verdict.BAN})

    assert harness.core.clear_verdict(subject) is None
    assert harness.core.edit_verdict(subject, Verdict.FAVOURITE) is None

    assert harness.core.resolve_verdicts([subject])[subject] == ResolvedVerdict(
        verdict=Verdict.FAVOURITE, value=100
    )


def test_a_clearance_of_the_only_verdict_leaves_nothing_standing(db_path: Path) -> None:
    """A **Wallpaper** with no **Ignores** at all, **Favourited** from **History** and then **Cleared**,
    resolves to the same nothing it did before anyone touched it.

    It still has a **History** row — two entries are two facts — but nothing about it now stands.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    subject = batch.wallpapers[0].id
    assert harness.core.edit_verdict(subject, Verdict.FAVOURITE) is None

    assert harness.core.clear_verdict(subject) is None

    assert harness.core.resolve_verdicts([subject])[subject] == ResolvedVerdict(verdict=None, value=0)
    assert len(harness.core.list_history(wallpaper_id=subject)) == 2


def test_a_clearance_and_a_verdict_sharing_a_timestamp_resolve_by_sequence(db_path: Path) -> None:
    """Invariant 4, now literally reachable rather than tested by proxy.

    #3 could only approximate this — two submitted **Batches** under a frozen clock — because nothing then
    wrote two entries for one **Wallpaper** that a user could have made seconds apart. **History** does: a
    **Clearance** and an edit are two clicks, and under wallpapi's frozen-second granularity they share a
    `recorded_at` exactly as entries from one transaction do. Ordering by timestamp here would be a coin
    toss between "cleared" and "liked", and the two answers are entirely different.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    subject = first.wallpapers[0].id
    submit_with(harness, {subject: Verdict.FAVOURITE})

    harness.core.clear_verdict(subject)
    harness.core.edit_verdict(subject, Verdict.LIKE)

    entries = harness.core.list_history(wallpaper_id=subject)
    assert [e.entry for e in entries] == [Verdict.FAVOURITE, Clearance.CLEARED, Verdict.LIKE]
    assert len({e.recorded_at for e in entries}) == 1, "the clock must not have moved"
    assert harness.core.resolve_verdicts([subject])[subject].verdict is Verdict.LIKE


def test_clearing_a_ban_makes_the_wallpaper_eligible_for_a_batch_again(db_path: Path) -> None:
    """Acceptance criterion: un-**Banning** from **History** puts the **Wallpaper** back in circulation.

    Nothing in **Batch** building knows the word. It excludes by *resolved* **Verdict**, so withdrawing the
    **Ban** is the whole of the change. With a catalogue of one, "eligible again" is the difference between
    a **Batch unavailable** page and a **Batch**.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    banned = batch.wallpapers[0].id
    harness.core.set_draft_verdict(batch.id, banned, Verdict.BAN)

    assert isinstance(harness.core.submit_batch(batch.id), BatchUnavailable), "the Ban must exclude it"
    assert harness.core.clear_verdict(banned) is None

    reoffered = harness.core.get_next_batch()
    assert isinstance(reoffered, Batch)
    assert [w.id for w in reoffered.wallpapers] == [banned]


def test_banning_from_history_keeps_the_wallpaper_out_of_the_next_batch(db_path: Path) -> None:
    """The other direction, and the acceptance criterion that an edit changes what happens next.

    The **Ban** is given from **History** rather than to a **Batch**, and the next **Batch** is built
    without the **Wallpaper** — which is what "editing a **Verdict** changes **Scores** immediately" means
    for the only consumer of resolution that exists today.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1)
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    subject = first.wallpapers[0].id
    harness.core.submit_batch(first.id)

    assert harness.core.edit_verdict(subject, Verdict.BAN) is None

    live = harness.core.get_next_batch()
    assert isinstance(live, Batch)
    harness.core.submit_batch(live.id)
    assert isinstance(harness.core.get_next_batch(), BatchUnavailable)


def test_an_edit_appends_and_never_rewrites(db_path: Path) -> None:
    """Acceptance criterion: edits are appended. The earlier entry is still there, with its **Batch**.

    An edit from **History** carries no `batch_id`, which is the only thing in the log that tells the two
    apart — and is what keeps a **History** edit from being counted among what a **Batch** recorded.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    subject = first.wallpapers[0].id
    submitted = submit_with(harness, {subject: Verdict.LIKE})

    assert harness.core.edit_verdict(subject, Verdict.BAN) is None

    entries = harness.core.list_history(wallpaper_id=subject)
    assert [(e.entry, e.batch_id) for e in entries] == [
        (Verdict.LIKE, submitted.id),
        (Verdict.BAN, None),
    ]
    from_the_batch = harness.core.list_history(batch_id=submitted.id, wallpaper_id=subject)
    assert [e.entry for e in from_the_batch] == [Verdict.LIKE], "the edit is not part of the Batch"


def test_editing_to_ignore_is_refused_and_appends_nothing(db_path: Path) -> None:
    """An **Ignore** is derived for everything unmarked at submit; it is never chosen.

    Choosing one from **History** would be a stored **Ignore** that stacks, standing next to a
    **Clearance** that means the opposite, and resolution would have two shapes of "no judgement".
    """
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    subject = first.wallpapers[0].id
    submit_with(harness, {subject: Verdict.LIKE})
    before = harness.core.list_history()

    refused = harness.core.edit_verdict(subject, Verdict.IGNORE)

    assert refused == HistoryRefused(reason=HistoryRefused.Reason.IGNORE_NOT_CHOOSABLE)
    assert harness.core.list_history() == before
    assert harness.core.resolve_verdicts([subject])[subject].verdict is Verdict.LIKE


def test_editing_an_unknown_wallpaper_is_refused_and_appends_nothing(harness: Harness) -> None:
    """A refusal in the house style rather than the foreign key's `IntegrityError`."""
    before = harness.core.list_history()

    refused = harness.core.edit_verdict("never-seen", Verdict.FAVOURITE)

    assert refused == HistoryRefused(reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER)
    assert harness.core.list_history() == before
    assert harness.library.written == []


def test_clearing_an_unknown_wallpaper_is_refused(harness: Harness) -> None:
    """Unknown outranks "nothing to clear": there is no **Wallpaper**, never mind a **Verdict** on it."""
    assert harness.core.clear_verdict("never-seen") == HistoryRefused(
        reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER
    )


def test_clearing_with_nothing_standing_is_refused_and_appends_nothing(db_path: Path) -> None:
    """Never judged, only **Ignored**, and already **Cleared** are all the same answer: there is no
    **Explicit Verdict** to withdraw, and an entry that changes nothing does not belong in an append-only
    log."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    never_judged, only_ignored = (w.id for w in first.wallpapers[:2])
    submit_with(harness, {})

    assert harness.core.clear_verdict(only_ignored) == HistoryRefused(
        reason=HistoryRefused.Reason.NOTHING_TO_CLEAR
    )
    harness.core.edit_verdict(never_judged, Verdict.LIKE)
    assert harness.core.clear_verdict(never_judged) is None
    before = harness.core.list_history()

    assert harness.core.clear_verdict(never_judged) == HistoryRefused(
        reason=HistoryRefused.Reason.NOTHING_TO_CLEAR
    )
    assert harness.core.list_history() == before


def test_favouriting_from_history_writes_the_library_file(db_path: Path, tmp_path: Path) -> None:
    """Acceptance criterion: the **Library** follows a **History** edit exactly as it follows a submission.

    Nothing in the **Library** code knows **History** exists — it is derived from the **Decision log**
    (ADR 0006), so appending the entry is the whole of the change.
    """
    library_path = tmp_path / "Library"
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=library_path)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = batch.wallpapers[0]
    submit_with(harness, {})

    assert harness.core.edit_verdict(shown.id, Verdict.FAVOURITE) is None

    assert len(harness.library.written) == 1
    assert harness.library.written[0].source_url == shown.full_url
    assert harness.library.written[0].destination == library_path / f"{shown.id}.jpg"


def test_downgrading_a_favourite_from_history_removes_its_library_file(db_path: Path, tmp_path: Path) -> None:
    """Acceptance criterion: removing a **Favourite** from **History** removes its file — and the path
    deleted is the one recorded when it was written, never one recomputed here (invariant 9)."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    submit_with(harness, {batch.wallpapers[0].id: Verdict.FAVOURITE})
    written = harness.library.written[0].destination

    assert harness.core.edit_verdict(batch.wallpapers[0].id, Verdict.LIKE) is None

    assert harness.library.removed == [written]


def test_clearing_a_favourite_removes_its_library_file(db_path: Path, tmp_path: Path) -> None:
    """The **Clearance** half of the same criterion: a **Cleared Favourite** is not a **Favourite**."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    submit_with(harness, {batch.wallpapers[0].id: Verdict.FAVOURITE})
    written = harness.library.written[0].destination

    assert harness.core.clear_verdict(batch.wallpapers[0].id) is None

    assert harness.library.removed == [written]
    assert len(harness.library.written) == 1, "nothing is re-downloaded by a clearance"


def test_a_clearance_survives_a_restart(db_path: Path) -> None:
    """The **Decision log** is the single source of truth, and a **Clearance** is one of its entries.

    A second Core service over the same file must read `'cleared'` back as a **Clearance** rather than
    raising on a **Verdict** it does not recognise — the trap every reader of the column has to avoid.
    """
    catalogue = (wallpaper("keeper"),)
    harness = make_harness(db_path, catalogue=catalogue)
    harness.core.update_settings(batch_size=1)
    submit_with(harness, {"keeper": Verdict.BAN})
    harness.core.clear_verdict("keeper")

    restarted = make_harness(db_path, catalogue=catalogue, fill_pool=0)

    assert [e.entry for e in restarted.core.list_history(wallpaper_id="keeper")] == [
        Verdict.BAN,
        Clearance.CLEARED,
    ]
    assert restarted.core.resolve_verdicts(["keeper"])["keeper"].verdict is None
