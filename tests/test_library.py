"""**Favourites** landing in the **Library**, through `library` alone with the fake writer.

The **Library** is derived from the **Decision log**: a **Favourite** with no file gets one, and a file whose
**Wallpaper** is no longer a **Favourite** loses it. That makes `reconcile` idempotent, which is what lets a
failed download simply be retried. A real in-memory database; **Wallpapers** are admitted and **Verdicts**
appended as `pool` and `decisions` do. The last section is the workflows calling it at the right moments.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import FIXED_NOW, Harness, LibraryRig, library_rig, live, make_harness
from tests.fakes import FakeLibraryWriter, catalogue_of, wallpaper
from wallpapi import decisions, settings, workflows
from wallpapi.library import Library
from wallpapi.model import Verdict

ONE, TWO, THREE = (w.id for w in catalogue_of(3))


@pytest.fixture
def writer() -> FakeLibraryWriter:
    return FakeLibraryWriter()


@pytest.fixture
def rig(tmp_path: Path, writer: FakeLibraryWriter) -> Iterator[LibraryRig]:
    with library_rig(tmp_path, writer, catalogue_of(3)) as made:
        yield made


def test_a_favourite_is_downloaded_into_the_library_path(rig: LibraryRig, writer: FakeLibraryWriter) -> None:
    """The full-resolution image, never the thumbnail, under the Wallhaven ID and the URL's extension."""
    rig.decide(Verdict.FAVOURITE, ONE)

    reconciled = rig.reconcile()

    assert reconciled.written == (ONE,)
    (written,) = writer.written
    assert written.wallpaper_id == ONE
    assert written.source_url == wallpaper(ONE).full_url
    assert written.destination == (rig.library_path / f"{ONE}.jpg").resolve()
    assert writer.removed == []


def test_likes_bans_and_ignores_never_write(rig: LibraryRig, writer: FakeLibraryWriter) -> None:
    rig.decide(Verdict.LIKE, ONE)
    rig.decide(Verdict.BAN, TWO)
    rig.decide(Verdict.IGNORE, THREE)

    rig.reconcile()

    assert writer.written == []
    assert writer.removed == []


@pytest.mark.parametrize("replacement", [Verdict.LIKE, Verdict.IGNORE, Verdict.BAN])
def test_replacing_a_favourite_removes_the_file_wallpapi_wrote(
    rig: LibraryRig, writer: FakeLibraryWriter, replacement: Verdict
) -> None:
    """Nothing is downloaded again for it, either."""
    rig.decide(Verdict.FAVOURITE, ONE)
    rig.reconcile()
    written = writer.written[0].destination

    rig.decide(replacement, ONE)

    assert rig.reconcile().removed == (ONE,)
    assert writer.removed == [written]
    assert len(writer.written) == 1


def test_a_removal_takes_its_recorded_path_and_nothing_else(
    rig: LibraryRig, writer: FakeLibraryWriter
) -> None:
    """Clearing the **Library** and rewriting it would pass the test above and fail this one. Two files is
    also what shows the path deleted is the recorded row's (invariant 9), not one recomputed here."""
    rig.decide(Verdict.FAVOURITE, ONE, TWO)
    rig.reconcile()
    recorded = {w.wallpaper_id: w.destination for w in writer.written}

    rig.decide(Verdict.BAN, TWO)
    rig.reconcile()

    assert writer.removed == [recorded[TWO]]
    assert recorded[TWO] != recorded[ONE]


def test_a_write_that_fails_is_reported_and_retried_by_the_next_reconciliation(
    rig: LibraryRig, writer: FakeLibraryWriter
) -> None:
    """Collected rather than raised, with no bookkeeping of its own: the **Favourite** with no file is the
    record of what is left to do."""
    rig.decide(Verdict.FAVOURITE, ONE)
    writer.fail_for.add(ONE)

    assert rig.reconcile().failed == (ONE,)
    assert writer.written == []

    writer.fail_for.clear()

    assert rig.reconcile().written == (ONE,)
    assert [w.wallpaper_id for w in writer.written] == [ONE]


def test_reconciling_again_writes_nothing_new(rig: LibraryRig, writer: FakeLibraryWriter) -> None:
    rig.decide(Verdict.FAVOURITE, ONE)
    rig.reconcile()

    first = rig.reconcile()
    second = rig.reconcile()

    assert first == second
    assert (first.written, first.removed, first.failed) == ((), (), ())
    assert len(writer.written) == 1


def test_the_record_outlives_the_library_that_wrote_it(rig: LibraryRig, writer: FakeLibraryWriter) -> None:
    """A restart: the recorded path is in the database, not in memory."""
    rig.decide(Verdict.FAVOURITE, ONE)
    rig.reconcile()
    written = writer.written[0].destination
    restarted = FakeLibraryWriter()

    rig.decide(Verdict.LIKE, ONE)
    Library(restarted, rig.clock).reconcile(rig.connection, rig.library_path)

    assert restarted.written == []
    assert restarted.removed == [written]


def test_favouriting_again_after_a_removal_writes_the_file_afresh(
    rig: LibraryRig, writer: FakeLibraryWriter
) -> None:
    rig.decide(Verdict.FAVOURITE, ONE)
    rig.reconcile()
    rig.decide(Verdict.LIKE, ONE)
    rig.reconcile()

    rig.decide(Verdict.FAVOURITE, ONE)
    rig.reconcile()

    assert [w.wallpaper_id for w in writer.written] == [ONE, ONE]
    assert writer.removed == [writer.written[0].destination]


def test_a_file_is_recorded_as_written_at_utc_from_the_clock(rig: LibraryRig) -> None:
    """Invariant 5: `written_at` is an ISO 8601 UTC string from the injected clock, never the machine's."""
    rig.clock.advance(3600)
    rig.decide(Verdict.FAVOURITE, ONE)

    rig.reconcile()

    (stored,) = [row["written_at"] for row in rig.connection.execute("SELECT written_at FROM library_files")]
    assert stored == (FIXED_NOW + dt.timedelta(hours=1)).isoformat()
    assert dt.datetime.fromisoformat(stored).tzinfo == dt.UTC


# -- a Library path that moved: the guard through the seam ------------------------------------------


def test_a_recorded_path_outside_the_library_is_dropped_rather_than_deleted(
    rig: LibraryRig, writer: FakeLibraryWriter, tmp_path: Path
) -> None:
    """The **Library path** changed, so the old file is no longer wallpapi's to delete. The row goes, so
    the refusal settles rather than repeating on every reconciliation, and a fresh **Favourite** is
    written where the path points today. On a real disk the file stays: `test_library_disk.py`."""
    original = rig.library_path
    rig.decide(Verdict.FAVOURITE, ONE)
    rig.reconcile()
    rig.library_path = tmp_path / "Moved"

    rig.decide(Verdict.LIKE, ONE)
    first = rig.reconcile()
    second = rig.reconcile()

    assert writer.removed == []
    assert first == second
    assert (first.written, first.removed, first.failed) == ((), (), ())

    rig.decide(Verdict.FAVOURITE, ONE)
    rig.reconcile()

    assert [w.wallpaper_id for w in writer.written] == [ONE, ONE]
    assert writer.written[0].destination.parent == original.resolve()
    assert writer.written[-1].destination == (rig.library_path / f"{ONE}.jpg").resolve()
    assert writer.removed == []


def test_a_wallhaven_id_that_cannot_name_a_file_is_a_failure_and_no_download(tmp_path: Path) -> None:
    """Refused before any download. The **Favourite** stands and is reported as failed: taste does not
    depend on wallpapi being able to name a file."""
    writer = FakeLibraryWriter()
    with library_rig(tmp_path, writer, (wallpaper("wp/0001"),)) as rig:
        rig.decide(Verdict.FAVOURITE, "wp/0001")

        pulled = rig.download_favourites()

        assert writer.written == []
        assert pulled.written == ()
        assert pulled.failed == ("wp/0001",)
        assert rig.reconcile().failed == ("wp/0001",)


# -- pulling every Favourite back down ---------------------------------------------------------------


def test_a_favourite_whose_file_is_not_on_this_disk_is_downloaded_again(
    rig: LibraryRig, writer: FakeLibraryWriter
) -> None:
    """ "I move devices and bring the db." The row is right and the file is simply not here, which is what
    the fake writer's paths always are: reconciling finds nothing to do and the download finds it all."""
    rig.decide(Verdict.FAVOURITE, ONE, TWO)
    rig.reconcile()
    assert rig.reconcile().written == ()

    pulled = rig.download_favourites()

    assert sorted(pulled.written) == [ONE, TWO]
    assert (pulled.skipped, pulled.failed) == ((), ())
    assert sorted(w.wallpaper_id for w in writer.written) == [ONE, ONE, TWO, TWO]


def test_downloading_favourites_writes_only_favourites_and_never_deletes(
    rig: LibraryRig, writer: FakeLibraryWriter
) -> None:
    """One way only, and **Verdict resolution** decides what is a **Favourite**: one taken back, and one
    never given, are neither written nor touched again."""
    rig.decide(Verdict.FAVOURITE, ONE, TWO)
    rig.decide(Verdict.IGNORE, THREE)
    rig.reconcile()
    rig.decide(Verdict.LIKE, TWO)
    rig.reconcile()
    removed_by_the_edit = list(writer.removed)

    pulled = rig.download_favourites()

    assert pulled.written == (ONE,)
    assert writer.removed == removed_by_the_edit


def test_a_download_that_fails_is_reported_and_retried_by_the_next_run(
    rig: LibraryRig, writer: FakeLibraryWriter
) -> None:
    """Collected rather than raised. The **Favourite** with no file is the record of what is left to do, so
    pressing the button again is the whole retry."""
    rig.decide(Verdict.FAVOURITE, ONE, TWO)
    writer.fail_for.add(ONE)

    first = rig.download_favourites()

    assert first.failed == (ONE,)
    assert first.written == (TWO,)

    writer.fail_for.clear()
    second = rig.download_favourites()

    assert second.failed == ()
    assert sorted(second.written) == [ONE, TWO]


# -- the workflows call it after the Decision log commits ----------------------------------------------


def library_harness(db_path: Path, library_path: Path, count: int) -> Harness:
    harness = make_harness(db_path, catalogue=catalogue_of(count))
    workflows.save_settings(harness.modules, batch_size=count, library_path=library_path)
    return harness


def test_a_submitted_favourite_is_written_after_the_decision_log_commits(
    db_path: Path, tmp_path: Path
) -> None:
    """A failed download cannot roll the **Decision log** back; the next submission's reconciliation picks
    the **Favourite** up."""
    harness = library_harness(db_path, tmp_path / "Library", 2)
    batch = live(harness)
    failing, written = (w.id for w in batch.wallpapers)
    harness.library.fail_for.add(failing)
    workflows.set_all_drafts(harness.modules, batch.id, Verdict.FAVOURITE)

    workflows.submit(harness.modules, batch.id)

    assert [w.wallpaper_id for w in harness.library.written] == [written]
    assert [e.entry for e in decisions.entries(harness.connect(), batch_id=batch.id)] == [
        Verdict.FAVOURITE
    ] * 2

    harness.library.fail_for.clear()

    assert harness.modules.library.reconcile(
        harness.connect(), settings.get(harness.connect()).library_path
    ).written == (failing,)


def test_a_history_edit_takes_the_file_away(db_path: Path, tmp_path: Path) -> None:
    harness = library_harness(db_path, tmp_path / "Library", 1)
    batch = live(harness)
    workflows.set_all_drafts(harness.modules, batch.id, Verdict.FAVOURITE)
    workflows.submit(harness.modules, batch.id)
    shown = batch.wallpapers[0].id

    assert workflows.edit_verdict(harness.modules, shown, Verdict.LIKE) is None

    assert harness.library.removed == [harness.library.written[0].destination]
