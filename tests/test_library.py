"""**Favourites** landing in the **Library**, driven through the Core service with the fake writer.

The **Library** is derived from the **Decision log**, not written as a side effect of a click: a
**Favourite** with no file gets one, and a file whose **Wallpaper** is no longer a **Favourite** loses it.
That makes `reconcile_library()` idempotent, which is what lets a failed download simply be retried.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import Harness, make_harness
from tests.fakes import catalogue_of
from wallpapi.core import Batch
from wallpapi.model import Verdict


def favourite_the_whole_batch(harness: Harness, verdict: Verdict = Verdict.FAVOURITE) -> Batch:
    """Mark every **Wallpaper** on the live **Batch** and submit it, returning the **Batch** submitted."""
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    harness.core.set_all_draft_verdicts(batch.id, verdict)
    harness.core.submit_batch(batch.id)
    return batch


def test_a_favourite_is_downloaded_into_the_configured_library_path(db_path: Path, tmp_path: Path) -> None:
    """The acceptance criterion at the heart of the ticket.

    One **Favourite**, one write, of the full-resolution image — `full_url` and never `thumbnail_url` —
    into the **Library** folder the settings name, under the Wallhaven ID and the URL's own extension.
    """
    library_path = tmp_path / "Library"
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=library_path)

    batch = favourite_the_whole_batch(harness)
    shown = batch.wallpapers[0]

    assert len(harness.library.written) == 1
    written = harness.library.written[0]
    assert written.wallpaper_id == shown.id
    assert written.source_url == shown.full_url
    assert written.destination == library_path / f"{shown.id}.jpg"
    assert harness.library.removed == []


def test_likes_bans_and_ignores_never_write(db_path: Path, tmp_path: Path) -> None:
    """The **Library** is favourites-only. A **Like** is recorded in **History** and downloads nothing."""
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    harness.core.update_settings(batch_size=3, library_path=tmp_path / "Library")
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    liked, banned, _ignored = (w.id for w in batch.wallpapers)
    harness.core.set_draft_verdict(batch.id, liked, Verdict.LIKE)
    harness.core.set_draft_verdict(batch.id, banned, Verdict.BAN)

    harness.core.submit_batch(batch.id)

    assert harness.library.written == []
    assert harness.library.removed == []


def test_replacing_a_favourite_removes_the_file_wallpapi_wrote(db_path: Path, tmp_path: Path) -> None:
    """A **Favourite** replaced by another **Explicit Verdict** is no longer a **Favourite**.

    **Verdict resolution** says the latest **Explicit Verdict** wins, so the file has to go — and the path
    deleted is the one recorded when it was written, never one recomputed here (invariant 9).
    """
    library_path = tmp_path / "Library"
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=library_path)
    first = favourite_the_whole_batch(harness)
    written = harness.library.written[0].destination

    favourite_the_whole_batch(harness, Verdict.LIKE)

    assert harness.library.removed == [written]
    assert len(harness.library.written) == 1
    assert harness.core.resolve_verdicts([first.wallpapers[0].id])[first.wallpapers[0].id].verdict is (
        Verdict.LIKE
    )


def test_a_ban_removes_the_file_too_and_nothing_else_is_removed(db_path: Path, tmp_path: Path) -> None:
    """Two **Favourites**, one of them later **Banned**: only that one's file goes.

    The obvious wrong implementation — clear the **Library** and rewrite it — passes the test above and
    fails this one.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    harness.core.update_settings(batch_size=2, library_path=tmp_path / "Library")
    first = favourite_the_whole_batch(harness)
    kept, banned = (w.id for w in first.wallpapers)
    written = {w.wallpaper_id: w.destination for w in harness.library.written}

    second = harness.core.get_next_batch()
    assert isinstance(second, Batch)
    harness.core.set_draft_verdict(second.id, banned, Verdict.BAN)
    harness.core.submit_batch(second.id)

    assert harness.library.removed == [written[banned]]
    assert sorted(w.wallpaper_id for w in harness.library.written) == sorted([kept, banned])
    assert harness.core.resolve_verdicts([kept])[kept].verdict is Verdict.FAVOURITE


def test_a_write_that_fails_leaves_the_decision_log_intact_and_is_retried(
    db_path: Path, tmp_path: Path
) -> None:
    """A **Favourite** is a fact about taste, not about a download succeeding.

    The download happens after the **Decision log** transaction has committed, so a failure cannot roll it
    back — and because the **Library** is derived rather than written once, the next reconciliation picks
    the **Favourite** up again with no bookkeeping of its own.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")
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
    """Idempotent, which is the property the retry above rests on.

    A **Favourite** that already has a recorded **Library** file is not downloaded a second time, however
    many times the reconciliation runs.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")
    favourite_the_whole_batch(harness)

    first = harness.core.reconcile_library()
    second = harness.core.reconcile_library()

    assert first == second
    assert first.written == ()
    assert first.removed == ()
    assert len(harness.library.written) == 1


def test_deletion_targets_the_recorded_path_and_not_a_recomputed_one(db_path: Path, tmp_path: Path) -> None:
    """Invariant 9, stated as a behaviour.

    Two **Favourites** written into the **Library** under names of their own, then one of them replaced:
    the path deleted is the one recorded when that file was written, not one worked out here from the
    **Wallpaper** and the setting. The pair is what makes it an assertion — recomputing would delete a
    path that happened to be right, and the second file proves the right *row* was read.

    What the setting *changing* does is #14's business and lives in `test_library_guards.py`: a recorded
    path that no longer resolves inside the **Library** folder is dropped rather than deleted.
    """
    library_path = tmp_path / "Library"
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    harness.core.update_settings(batch_size=2, library_path=library_path)
    first = favourite_the_whole_batch(harness)
    kept, replaced = (w.id for w in first.wallpapers)
    recorded = {w.wallpaper_id: w.destination for w in harness.library.written}

    second = harness.core.get_next_batch()
    assert isinstance(second, Batch)
    harness.core.set_draft_verdict(second.id, replaced, Verdict.LIKE)
    harness.core.submit_batch(second.id)

    assert harness.library.removed == [recorded[replaced]]
    assert recorded[replaced] != recorded[kept]


def test_a_restarted_core_service_still_knows_the_recorded_path(db_path: Path, tmp_path: Path) -> None:
    """The recorded path is storage, not memory: a new Core service over the same database still has it."""
    library_path = tmp_path / "Library"
    first_run = make_harness(db_path, catalogue=catalogue_of(1))
    first_run.core.update_settings(batch_size=1, library_path=library_path)
    favourite_the_whole_batch(first_run)
    written = first_run.library.written[0].destination

    second_run = make_harness(db_path, catalogue=catalogue_of(1))
    favourite_the_whole_batch(second_run, Verdict.LIKE)

    assert second_run.library.written == []
    assert second_run.library.removed == [written]


def test_favouriting_again_after_a_removal_writes_the_file_afresh(db_path: Path, tmp_path: Path) -> None:
    """Falls out of the **Library** following the **Decision log** rather than being written once.

    Nothing here is special-cased: the **Wallpaper** resolves to **Favourite** again and has no recorded
    file, which is the same condition as the very first write.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")
    first = favourite_the_whole_batch(harness)
    shown = first.wallpapers[0].id
    favourite_the_whole_batch(harness, Verdict.LIKE)

    favourite_the_whole_batch(harness)

    assert [w.wallpaper_id for w in harness.library.written] == [shown, shown]
    assert harness.library.removed == [harness.library.written[0].destination]


# -- one-way sync: pulling every Favourite back down (#14) -----------------------------------------------


def test_a_favourite_whose_file_is_not_on_this_disk_is_downloaded_again(
    db_path: Path, tmp_path: Path
) -> None:
    """ "I move devices and bring the db" — the case the whole operation exists for.

    The **Decision log** says **Favourite** and the `library_files` row says where the file was written on
    the machine that wrote it. Nothing about that row is wrong; the file is simply not here. The fake
    writer stands in for exactly that, because the paths it records are never on this disk — so a
    reconciliation still finds nothing to do and `download_favourites` finds everything to do.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    harness.core.update_settings(batch_size=2, library_path=tmp_path / "Library")
    batch = favourite_the_whole_batch(harness)
    favourited = sorted(w.id for w in batch.wallpapers)
    assert harness.core.reconcile_library().written == ()

    pulled = harness.core.download_favourites()

    assert sorted(pulled.written) == favourited
    assert pulled.skipped == ()
    assert pulled.failed == ()
    assert sorted(w.wallpaper_id for w in harness.library.written) == sorted(favourited * 2)


def test_downloading_favourites_writes_nothing_for_anything_that_is_not_one(
    db_path: Path, tmp_path: Path
) -> None:
    """**Favourites** only, and **Verdict resolution** is what decides which those are.

    A **Liked**, **Banned** or **Ignored** **Wallpaper** is not in the **Library** and is not put there by
    asking for the **Library** back.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(3))
    harness.core.update_settings(batch_size=3, library_path=tmp_path / "Library")
    batch = harness.core.get_next_batch()
    assert isinstance(batch, Batch)
    favourited, liked, _ignored = (w.id for w in batch.wallpapers)
    harness.core.set_draft_verdict(batch.id, favourited, Verdict.FAVOURITE)
    harness.core.set_draft_verdict(batch.id, liked, Verdict.LIKE)
    harness.core.submit_batch(batch.id)

    pulled = harness.core.download_favourites()

    assert pulled.written == (favourited,)
    assert {w.wallpaper_id for w in harness.library.written} == {favourited}


def test_downloading_favourites_never_deletes_anything(db_path: Path, tmp_path: Path) -> None:
    """One way only. This operation is the one place wallpapi looks at the folder, and it only ever adds.

    The **Wallpaper** whose **Favourite** was replaced lost its file when the **Batch** was submitted; the
    download does not touch it again, and nothing else is removed on the way past either.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    harness.core.update_settings(batch_size=2, library_path=tmp_path / "Library")
    first = favourite_the_whole_batch(harness)
    kept, replaced = (w.id for w in first.wallpapers)
    second = harness.core.get_next_batch()
    assert isinstance(second, Batch)
    harness.core.set_draft_verdict(second.id, replaced, Verdict.LIKE)
    harness.core.submit_batch(second.id)
    removed_by_the_submission = list(harness.library.removed)

    pulled = harness.core.download_favourites()

    assert pulled.written == (kept,)
    assert harness.library.removed == removed_by_the_submission


def test_a_download_that_fails_is_reported_and_retried_by_the_next_run(db_path: Path, tmp_path: Path) -> None:
    """Failures are collected rather than raised, and the retry needs no bookkeeping of its own.

    The settings page says how many failed; what makes that enough is that the **Favourite** with no file
    *is* the record of what still has to happen, so pressing the button again is the whole retry.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(2))
    harness.core.update_settings(batch_size=2, library_path=tmp_path / "Library")
    batch = favourite_the_whole_batch(harness)
    stubborn, fine = (w.id for w in batch.wallpapers)
    harness.library.fail_for.add(stubborn)

    first = harness.core.download_favourites()

    assert first.failed == (stubborn,)
    assert first.written == (fine,)

    harness.library.fail_for.clear()
    second = harness.core.download_favourites()

    assert second.failed == ()
    assert sorted(second.written) == sorted([stubborn, fine])
