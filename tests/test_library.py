"""**Favourites** landing in the **Library**, driven through the Core service with the fake writer.

The **Library** is derived from the **Decision log**: a **Favourite** with no file gets one, and a file whose
**Wallpaper** is no longer a **Favourite** loses it. That makes `reconcile_library()` idempotent, which is
what lets a failed download simply be retried.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Harness, favourite_the_whole_batch, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import Batch
from wallpapi.model import Verdict


def library_harness(db_path: Path, library_path: Path, count: int) -> Harness:
    harness = make_harness(db_path, catalogue=catalogue_of(count))
    harness.core.update_settings(batch_size=count, library_path=library_path)
    return harness


def test_a_favourite_is_downloaded_into_the_configured_library_path(db_path: Path, tmp_path: Path) -> None:
    """The full-resolution image, never the thumbnail, under the Wallhaven ID and the URL's extension."""
    library_path = tmp_path / "Library"
    harness = library_harness(db_path, library_path, 1)

    shown = favourite_the_whole_batch(harness).wallpapers[0]

    assert len(harness.library.written) == 1
    written = harness.library.written[0]
    assert written.wallpaper_id == shown.id
    assert written.source_url == shown.full_url
    assert written.destination == library_path / f"{shown.id}.jpg"
    assert harness.library.removed == []


def test_likes_bans_and_ignores_never_write(db_path: Path, tmp_path: Path) -> None:
    harness = library_harness(db_path, tmp_path / "Library", 3)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    liked, banned, _ignored = (w.id for w in batch.wallpapers)
    harness.core.set_draft_verdict(batch.id, liked, Verdict.LIKE)
    harness.core.set_draft_verdict(batch.id, banned, Verdict.BAN)

    harness.core.submit_batch(batch.id)

    assert harness.library.written == []
    assert harness.library.removed == []


@pytest.mark.parametrize("replacement", [Verdict.LIKE, Verdict.IGNORE])
def test_replacing_a_favourite_removes_the_file_wallpapi_wrote(
    db_path: Path, tmp_path: Path, replacement: Verdict
) -> None:
    """Nothing is downloaded again for it, either."""
    harness = library_harness(db_path, tmp_path / "Library", 1)
    shown = favourite_the_whole_batch(harness).wallpapers[0].id
    written = harness.library.written[0].destination

    assert harness.core.edit_verdict(shown, replacement) is None

    assert harness.library.removed == [written]
    assert len(harness.library.written) == 1
    assert harness.core.resolve_verdicts([shown])[shown].verdict is replacement


def test_a_ban_removes_that_file_from_its_recorded_path_and_nothing_else(
    db_path: Path, tmp_path: Path
) -> None:
    """Clearing the **Library** and rewriting it would pass the test above and fail this one. Two files is
    also what shows the path deleted is the recorded row's (invariant 9), not one recomputed here."""
    harness = library_harness(db_path, tmp_path / "Library", 2)
    kept, banned = (w.id for w in favourite_the_whole_batch(harness).wallpapers)
    recorded = {w.wallpaper_id: w.destination for w in harness.library.written}

    assert harness.core.edit_verdict(banned, Verdict.BAN) is None

    assert harness.library.removed == [recorded[banned]]
    assert recorded[banned] != recorded[kept]
    assert harness.core.resolve_verdicts([kept])[kept].verdict is Verdict.FAVOURITE


def test_a_write_that_fails_leaves_the_decision_log_intact_and_is_retried(
    db_path: Path, tmp_path: Path
) -> None:
    """The download happens after the **Decision log** commits, so it cannot roll it back; and the next
    reconciliation picks the **Favourite** up again with no bookkeeping of its own."""
    harness = library_harness(db_path, tmp_path / "Library", 1)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = batch.wallpapers[0].id
    harness.library.fail_for.add(shown)

    harness.core.set_draft_verdict(batch.id, shown, Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)

    assert harness.library.written == []
    assert [e.entry for e in harness.core.list_history(batch_id=batch.id)] == [Verdict.FAVOURITE]
    assert harness.core.resolve_verdicts([shown])[shown].verdict is Verdict.FAVOURITE

    harness.library.fail_for.clear()
    reconciled = harness.core.reconcile_library()

    assert reconciled.written == (shown,)
    assert [w.wallpaper_id for w in harness.library.written] == [shown]


def test_reconciling_again_writes_nothing_new(db_path: Path, tmp_path: Path) -> None:
    harness = library_harness(db_path, tmp_path / "Library", 1)
    favourite_the_whole_batch(harness)

    first = harness.core.reconcile_library()
    second = harness.core.reconcile_library()

    assert first == second
    assert first.written == ()
    assert first.removed == ()
    assert len(harness.library.written) == 1


def test_a_restarted_core_service_still_knows_the_recorded_path(db_path: Path, tmp_path: Path) -> None:
    first_run = library_harness(db_path, tmp_path / "Library", 1)
    favourited = favourite_the_whole_batch(first_run).wallpapers[0].id
    written = first_run.library.written[0].destination

    second_run = make_harness(db_path, catalogue=catalogue_of(1))
    assert second_run.core.edit_verdict(favourited, Verdict.LIKE) is None

    assert second_run.library.written == []
    assert second_run.library.removed == [written]


def test_favouriting_again_after_a_removal_writes_the_file_afresh(db_path: Path, tmp_path: Path) -> None:
    harness = library_harness(db_path, tmp_path / "Library", 1)
    shown = favourite_the_whole_batch(harness).wallpapers[0].id
    assert harness.core.edit_verdict(shown, Verdict.LIKE) is None

    assert harness.core.edit_verdict(shown, Verdict.FAVOURITE) is None

    assert [w.wallpaper_id for w in harness.library.written] == [shown, shown]
    assert harness.library.removed == [harness.library.written[0].destination]


# -- a Library path that moved: the guard through the seam ------------------------------------------


def test_a_recorded_path_outside_the_library_is_dropped_rather_than_deleted(
    db_path: Path, tmp_path: Path
) -> None:
    """The **Library path** changed, so the old file is no longer wallpapi's to delete. The row goes, so
    the refusal settles rather than repeating on every reconciliation, and a fresh **Favourite** is
    written where the setting points today."""
    original = tmp_path / "Original"
    moved = tmp_path / "Moved"
    harness = library_harness(db_path, original, 1)
    shown = favourite_the_whole_batch(harness).wallpapers[0].id
    harness.core.update_settings(library_path=moved)

    assert harness.core.edit_verdict(shown, Verdict.LIKE) is None
    first = harness.core.reconcile_library()
    second = harness.core.reconcile_library()

    assert harness.library.removed == []
    assert first == second
    assert (first.written, first.removed, first.failed) == ((), (), ())

    assert harness.core.edit_verdict(shown, Verdict.FAVOURITE) is None

    assert [w.wallpaper_id for w in harness.library.written] == [shown, shown]
    assert harness.library.written[0].destination.parent == original.resolve()
    assert harness.library.written[-1].destination == (moved / f"{shown}.jpg").resolve()
    assert harness.library.removed == []


def test_a_wallhaven_id_that_cannot_name_a_file_is_a_failure_and_no_download(
    db_path: Path, tmp_path: Path
) -> None:
    """Refused before any download. The **Favourite** stands and is reported as failed: taste does not
    depend on wallpapi being able to name a file."""
    harness = make_harness(db_path, catalogue=(wallpaper("wp/0001"),))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")

    favourite_the_whole_batch(harness)
    pulled = harness.core.download_favourites()

    assert harness.library.written == []
    assert pulled.written == ()
    assert pulled.failed == ("wp/0001",)
    assert harness.core.reconcile_library().failed == ("wp/0001",)


# -- pulling every Favourite back down ---------------------------------------------------------------


def test_a_favourite_whose_file_is_not_on_this_disk_is_downloaded_again(
    db_path: Path, tmp_path: Path
) -> None:
    """ "I move devices and bring the db." The row is right and the file is simply not here, which is what
    the fake writer's paths always are: reconciling finds nothing to do and the download finds it all."""
    harness = library_harness(db_path, tmp_path / "Library", 2)
    favourited = sorted(w.id for w in favourite_the_whole_batch(harness).wallpapers)
    assert harness.core.reconcile_library().written == ()

    pulled = harness.core.download_favourites()

    assert sorted(pulled.written) == favourited
    assert pulled.skipped == ()
    assert pulled.failed == ()
    assert sorted(w.wallpaper_id for w in harness.library.written) == sorted(favourited * 2)


def test_downloading_favourites_writes_only_favourites_and_never_deletes(
    db_path: Path, tmp_path: Path
) -> None:
    """One way only, and **Verdict resolution** decides what is a **Favourite**: one taken back, and one
    never given, are neither written nor touched again."""
    harness = library_harness(db_path, tmp_path / "Library", 3)
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    kept, replaced, _ignored = (w.id for w in batch.wallpapers)
    harness.core.set_draft_verdict(batch.id, kept, Verdict.FAVOURITE)
    harness.core.set_draft_verdict(batch.id, replaced, Verdict.FAVOURITE)
    harness.core.submit_batch(batch.id)
    assert harness.core.edit_verdict(replaced, Verdict.LIKE) is None
    removed_by_the_edit = list(harness.library.removed)

    pulled = harness.core.download_favourites()

    assert pulled.written == (kept,)
    assert harness.library.removed == removed_by_the_edit


def test_a_download_that_fails_is_reported_and_retried_by_the_next_run(db_path: Path, tmp_path: Path) -> None:
    """Collected rather than raised. The **Favourite** with no file is the record of what is left to do, so
    pressing the button again is the whole retry."""
    harness = library_harness(db_path, tmp_path / "Library", 2)
    stubborn, fine = (w.id for w in favourite_the_whole_batch(harness).wallpapers)
    harness.library.fail_for.add(stubborn)

    first = harness.core.download_favourites()

    assert first.failed == (stubborn,)
    assert first.written == (fine,)

    harness.library.fail_for.clear()
    second = harness.core.download_favourites()

    assert second.failed == ()
    assert sorted(second.written) == sorted([stubborn, fine])
