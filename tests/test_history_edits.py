"""**History** edits: issue #7's **Core service** half, as #37 changed it.

Any **Verdict** can be changed to any other from **History**, **Ignore** included. An **Ignore** is how a
**Verdict** is withdrawn — the same entry unmarking a tile writes — and since the latest entry decides, it
overturns whatever stood. Nothing writes a **Clearance** any more.

Every test enters through the Core service, and every entry under test is made the way the app makes it:
by drafting and submitting, or by `edit_verdict`.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch, BatchUnavailable, HistoryRefused, ResolvedVerdict
from wallpapi.model import Verdict


def submit_with(harness: Harness, marks: dict[str, Verdict | None]) -> Batch:
    """Draft `marks` against the live **Batch** and submit it, returning the **Batch** submitted.

    The catalogues here hold exactly `batch_size` **Wallpapers**, so every **Batch** shows all of them. A
    tile left out of `marks` keeps what the **Batch** was minted with, and `None` unmarks it.
    """
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    for wallpaper_id, verdict in marks.items():
        harness.core.set_draft_verdict(batch.id, wallpaper_id, verdict)
    harness.core.submit_batch(batch.id)
    return batch


def test_an_ignore_from_history_overturns_the_verdict(db_path: Path) -> None:
    """The **History** half of #37: withdrawing a **Like** there is an **Ignore**, worth -10, with no
    **Batch** on the entry — not a return to never having been seen."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    subject = "wp0000"
    submit_with(harness, {subject: Verdict.LIKE})

    assert harness.core.edit_verdict(subject, Verdict.IGNORE) is None

    assert harness.core.resolve_verdicts([subject])[subject] == ResolvedVerdict(
        verdict=Verdict.IGNORE, value=-10
    )
    latest = harness.core.list_history(wallpaper_id=subject)[-1]
    assert (latest.entry, latest.batch_id) == (Verdict.IGNORE, None)


def test_a_verdict_after_an_ignore_wins(db_path: Path) -> None:
    """An **Ignore** is not a floor the **Wallpaper** is stuck on: judging it again decides outright."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = submit_with(harness, {})
    subject = first.wallpapers[0].id

    assert harness.core.edit_verdict(subject, Verdict.FAVOURITE) is None

    assert harness.core.resolve_verdicts([subject])[subject] == ResolvedVerdict(
        verdict=Verdict.FAVOURITE, value=100
    )


def test_two_history_edits_sharing_a_timestamp_resolve_by_sequence(db_path: Path) -> None:
    """Invariant 4, reachable through **History**: two edits are two clicks, and under wallpapi's
    whole-second clock they share a `recorded_at` exactly as entries from one transaction do. Ordering by
    timestamp would be a coin toss between "ignored" and "liked"."""
    harness = make_harness(db_path, catalogue=catalogue_of(8))
    first = harness.core.get_next_batch()
    assert isinstance(first, Batch)
    subject = first.wallpapers[0].id
    submit_with(harness, {subject: Verdict.FAVOURITE})

    harness.core.edit_verdict(subject, Verdict.IGNORE)
    harness.core.edit_verdict(subject, Verdict.LIKE)

    entries = harness.core.list_history(wallpaper_id=subject)
    assert [e.entry for e in entries] == [Verdict.FAVOURITE, Verdict.IGNORE, Verdict.LIKE]
    assert len({e.recorded_at for e in entries}) == 1, "the clock must not have moved"
    assert harness.core.resolve_verdicts([subject])[subject].verdict is Verdict.LIKE


def test_ignoring_a_ban_from_history_makes_the_wallpaper_eligible_again(db_path: Path) -> None:
    """Un-**Banning** falls out of the rule rather than being built. **Batch** building excludes by
    *resolved* **Verdict**, and a **Ban** overturned by an **Ignore** is no longer one. With a catalogue
    of one, "eligible again" is the difference between a **Batch unavailable** page and a **Batch**.

    Banned from **History** before any **Batch** showed it, because since #38 a **Wallpaper** a **Batch**
    has shown has left the **Pool** for good (ADR 0016) and no edit brings it back — so this is the one
    place left where an overturned **Ban** can still be drawn.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1)
    banned = "wp0000"
    assert harness.core.edit_verdict(banned, Verdict.BAN) is None
    assert isinstance(harness.core.get_next_batch(), BatchUnavailable), "the Ban must exclude it"

    assert harness.core.edit_verdict(banned, Verdict.IGNORE) is None

    reoffered = harness.core.get_next_batch()
    assert isinstance(reoffered, Batch)
    assert [w.id for w in reoffered.wallpapers] == [banned]
    assert reoffered.drafts == {}, "an Ignore is not pre-filled: the tile comes up unmarked"


def test_banning_from_history_keeps_the_wallpaper_out_of_the_next_batch(db_path: Path) -> None:
    """An edit changes what happens next: the **Ban** is given from **History** and the next **Batch**
    is built without the **Wallpaper**."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1)

    assert harness.core.edit_verdict("wp0000", Verdict.BAN) is None

    assert isinstance(harness.core.get_next_batch(), BatchUnavailable)


def test_a_history_edit_does_not_reach_the_batch_already_open(db_path: Path) -> None:
    """Deliberately not handled (ADR 0015). The **Batch** open during the edit was minted before it, so
    its tile is unmarked, and submitting it untouched records an **Ignore** — the latest entry, which
    overturns the edit. The tile shows what it will record; the user changes it there."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1)
    open_batch = harness.core.get_next_batch()
    assert isinstance(open_batch, Batch)

    assert harness.core.edit_verdict("wp0000", Verdict.LIKE) is None
    submitted = submit_with(harness, {})

    assert submitted == open_batch, "the edit neither rerolled nor re-marked the open Batch"

    assert harness.core.resolve_verdicts(["wp0000"])["wp0000"].verdict is Verdict.IGNORE


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


def test_editing_an_unknown_wallpaper_is_refused_and_appends_nothing(harness: Harness) -> None:
    """A refusal in the house style rather than the foreign key's `IntegrityError`."""
    before = harness.core.list_history()

    refused = harness.core.edit_verdict("never-seen", Verdict.IGNORE)

    assert refused == HistoryRefused(reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER)
    assert harness.core.list_history() == before
    assert harness.library.written == []


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


def test_ignoring_a_favourite_from_history_removes_its_library_file(db_path: Path, tmp_path: Path) -> None:
    """An **Ignored** **Favourite** is not a **Favourite**, and nothing is re-downloaded for it."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    submit_with(harness, {batch.wallpapers[0].id: Verdict.FAVOURITE})
    written = harness.library.written[0].destination

    assert harness.core.edit_verdict(batch.wallpapers[0].id, Verdict.IGNORE) is None

    assert harness.library.removed == [written]
    assert len(harness.library.written) == 1


def test_unmarking_a_pre_filled_favourite_removes_its_library_file(db_path: Path, tmp_path: Path) -> None:
    """The **Batch** half of the same rule (ADR 0015): a **Favourite** shown pre-marked and unmarked
    resolves to an **Ignore** at submission, and the **Library** follows the log.

    Favourited from **History** before any **Batch** showed it, which is the one way a decided
    **Wallpaper** is still drawn since #38 (ADR 0016); pre-marking is otherwise dormant until #52.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")
    subject = "wp0000"
    assert harness.core.edit_verdict(subject, Verdict.FAVOURITE) is None
    written = harness.library.written[0].destination
    reshown = harness.core.get_next_batch()
    assert isinstance(reshown, Batch)
    assert reshown.drafts == {subject: Verdict.FAVOURITE}

    submit_with(harness, {subject: None})

    assert harness.library.removed == [written]
