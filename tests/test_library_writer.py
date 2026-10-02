"""The real **Library** writer, against a temporary folder and a mocked transport rather than the network.

Three things only this writer can be asked: that the file arrives whole (invariant 10 — temp file inside
the **Library** folder, then `os.replace`), that deleting a **Library** file in Explorer changes nothing,
and — since #14 — that only a regular file is ever unlinked and that one download puts a deleted
**Library** back. All of them need real bytes on a real disk, so they are here rather than with the fake.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from tests.conftest import FIXED_NOW
from tests.fakes import FakeClock, FakeSimilarityProvider, FakeWallhavenClient, catalogue_of
from tests.test_library_guards import library_junction, library_symlink
from wallpapi.core import Batch, CoreService
from wallpapi.library import DownloadingLibraryWriter
from wallpapi.model import Verdict
from wallpapi.rng import SeededRandom

IMAGE_BYTES = b"\xff\xd8\xff\xe0 full resolution, allegedly"
"""What the mocked transport serves for any `w.wallhaven.cc` URL."""


def downloading_writer(served: bytes = IMAGE_BYTES) -> tuple[DownloadingLibraryWriter, list[httpx2.Request]]:
    """The real writer over a transport that answers from memory, plus the requests it made."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=served)

    return DownloadingLibraryWriter(client=httpx2.Client(transport=httpx2.MockTransport(handler))), seen


def core_with_a_real_writer(
    tmp_path: Path, *, catalogue_size: int = 1
) -> tuple[CoreService, list[httpx2.Request]]:
    """A Core service whose **Library** writer really writes, with the **Pool** already primed.

    A **Batch** is drawn from the **Pool** (#6), so the **Pool** has to have something in it. One refill
    step is one page, which is the fake client's whole catalogue here.
    """
    writer, seen = downloading_writer()
    core = CoreService(
        db_path=tmp_path / "wallpapi.db",
        wallhaven=FakeWallhavenClient(catalogue_of(catalogue_size)),
        library=writer,
        similarity=FakeSimilarityProvider(),
        random_source=SeededRandom(1),
        clock=FakeClock(FIXED_NOW),
    )
    core.refill_step()
    return core, seen


def test_the_first_write_creates_the_library_folder_and_leaves_no_part_file(tmp_path: Path) -> None:
    """The **Library** folder is created on the first write, and only the finished file is left in it.

    #4 deliberately stores the path without touching the filesystem, so the folder may well not exist. The
    temp file is a sibling inside that folder — `os.replace` is only atomic within one filesystem and
    raises across drives on Windows — and it must not survive the write.
    """
    destination = tmp_path / "Library" / "nested" / "wp0001.png"
    writer, seen = downloading_writer()

    written = writer.write("wp0001", "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.png", destination)

    assert written == destination
    assert written.read_bytes() == IMAGE_BYTES
    assert [request.url.path for request in seen] == ["/full/wp/wallhaven-wp0001.png"]
    assert [p.name for p in destination.parent.iterdir()] == [destination.name]


def test_a_second_write_replaces_the_file_in_place(tmp_path: Path) -> None:
    """`os.replace` overwrites rather than failing: a re-**Favourite** needs no separate delete first."""
    destination = tmp_path / "Library" / "wp0001.jpg"
    stale, _ = downloading_writer(b"stale")
    stale.write("wp0001", "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.jpg", destination)
    fresh, _ = downloading_writer()

    fresh.write("wp0001", "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.jpg", destination)

    assert destination.read_bytes() == IMAGE_BYTES
    assert [p.name for p in destination.parent.iterdir()] == [destination.name]


def test_removing_a_file_that_is_already_gone_is_a_no_op(tmp_path: Path) -> None:
    """Invariant 9: "**Library** file deleted in Explorer" is a state the spec guarantees is reachable."""
    writer, _ = downloading_writer()

    writer.remove(tmp_path / "Library" / "never-existed.jpg")

    assert not (tmp_path / "Library" / "never-existed.jpg").exists()


def test_a_download_that_dies_part_way_leaves_nothing_behind(tmp_path: Path) -> None:
    """The case atomicity exists for: half the bytes arrive and then the connection goes.

    Neither a truncated **Library** file nor the `.part` file it was being written as may survive, and the
    Core service must see the failure — a **Favourite** with no file is retried by the next reconciliation,
    a **Favourite** with half a file would not be.
    """
    destination = tmp_path / "Library" / "wp0001.jpg"

    def dies_part_way() -> Iterator[bytes]:
        yield b"the first half"
        raise httpx2.ReadError("the connection went away")

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, content=dies_part_way())

    writer = DownloadingLibraryWriter(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    with pytest.raises(httpx2.ReadError):
        writer.write("wp0001", "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.jpg", destination)

    assert list(destination.parent.iterdir()) == []


def test_deleting_a_library_file_in_explorer_changes_nothing(tmp_path: Path) -> None:
    """The acceptance criterion, with the real writer so there is a real file to delete.

    The **Library** is write-only and is never read back, so wallpapi has no way to notice the file went —
    and no business noticing. The **Decision log** and **Verdict resolution** are untouched, and
    reconciling again does not re-download it, because the recorded row still says the file is there.
    """
    library_path = tmp_path / "Library"
    core, seen = core_with_a_real_writer(tmp_path)
    core.update_settings(batch_size=1, library_path=library_path)
    batch = core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = batch.wallpapers[0].id
    core.set_draft_verdict(batch.id, shown, Verdict.FAVOURITE)
    core.submit_batch(batch.id)
    file = library_path / f"{shown}.jpg"
    assert file.read_bytes() == IMAGE_BYTES
    before = core.list_history()

    file.unlink()
    reconciled = core.reconcile_library()

    assert reconciled.written == ()
    assert reconciled.removed == ()
    assert reconciled.failed == ()
    assert not file.exists()
    assert len(seen) == 1
    assert core.list_history() == before
    assert core.resolve_verdicts([shown])[shown].verdict is Verdict.FAVOURITE


# -- only regular files are ever deleted (#14) -----------------------------------------------------------


def test_a_directory_is_never_removed(tmp_path: Path) -> None:
    """A row naming a folder is not a **Library** file, whatever it says, and is left alone.

    The Core service has already confined the path to the **Library** folder by the time it gets here,
    which makes this the folder the user chose — the one thing in the world that must survive a wrong row.
    """
    writer, _ = downloading_writer()
    folder = tmp_path / "Library" / "wp0001.jpg"
    folder.mkdir(parents=True)
    (folder / "something-of-the-users.txt").write_bytes(b"theirs")

    writer.remove(folder)

    assert folder.is_dir()
    assert (folder / "something-of-the-users.txt").exists()


def test_a_symlink_is_never_removed(tmp_path: Path) -> None:
    """Deleting a link deletes a name the user made, and wallpapi made no links."""
    library = tmp_path / "Library"
    target = tmp_path / "Elsewhere" / "precious.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not wallpapi's")
    library_symlink(library / "wp0001.jpg", target)
    writer, _ = downloading_writer()

    writer.remove(library / "wp0001.jpg")

    assert (library / "wp0001.jpg").is_symlink()
    assert target.read_bytes() == b"not wallpapi's"


def test_a_junction_is_never_removed(tmp_path: Path) -> None:
    """The reparse point Windows hands out without asking for a privilege, and the same answer."""
    library = tmp_path / "Library"
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    (outside / "precious.jpg").write_bytes(b"not wallpapi's")
    library.mkdir()
    library_junction(library / "wp0001.jpg", outside)
    writer, _ = downloading_writer()

    writer.remove(library / "wp0001.jpg")

    assert (outside / "precious.jpg").read_bytes() == b"not wallpapi's"
    assert (library / "wp0001.jpg").is_dir()


# -- pulling the Library back down, against a real folder (#14) ------------------------------------------


def test_one_download_brings_back_a_library_deleted_in_explorer(tmp_path: Path) -> None:
    """The acceptance criterion the maintainer asked for, end to end and on a real disk.

    "If I delete the file myself, or I move devices and bring the db, do not remove it from the
    **Decision log**; I want to pull all my **Favourites** with a single download." So: the
    **Decision log** is untouched by the deletion, a reconciliation still refuses to notice, and one call
    puts every missing file back — checking existence only for the paths wallpapi recorded itself.
    """
    library_path = tmp_path / "Library"
    core, seen = core_with_a_real_writer(tmp_path, catalogue_size=3)
    core.update_settings(batch_size=3, library_path=library_path)
    batch = core.get_next_batch()
    assert isinstance(batch, Batch)
    core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)
    core.submit_batch(batch.id)
    favourited = sorted(w.id for w in batch.wallpapers)
    assert sorted(p.name for p in library_path.iterdir()) == [f"{i}.jpg" for i in favourited]
    before = core.list_history()

    for file in library_path.iterdir():
        file.unlink()
    assert core.reconcile_library().written == ()
    pulled = core.download_favourites()

    assert sorted(pulled.written) == favourited
    assert pulled.failed == ()
    assert sorted(p.name for p in library_path.iterdir()) == [f"{i}.jpg" for i in favourited]
    assert all(p.read_bytes() == IMAGE_BYTES for p in library_path.iterdir())
    assert core.list_history() == before
    assert len(seen) == 6


def test_a_favourite_whose_file_is_still_there_is_skipped(tmp_path: Path) -> None:
    """The half that keeps the button cheap: only what is actually missing is fetched.

    Existence is checked for the recorded path and for nothing else — the folder is never listed, so a
    file the user put there by hand is neither noticed nor counted.
    """
    library_path = tmp_path / "Library"
    core, seen = core_with_a_real_writer(tmp_path, catalogue_size=2)
    core.update_settings(batch_size=2, library_path=library_path)
    batch = core.get_next_batch()
    assert isinstance(batch, Batch)
    core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)
    core.submit_batch(batch.id)
    kept, deleted = sorted(w.id for w in batch.wallpapers)
    (library_path / f"{deleted}.jpg").unlink()
    (library_path / "a-photo-of-the-users.jpg").write_bytes(b"theirs")

    pulled = core.download_favourites()

    assert pulled.written == (deleted,)
    assert pulled.skipped == (kept,)
    assert len(seen) == 3
    assert (library_path / "a-photo-of-the-users.jpg").read_bytes() == b"theirs"
    assert core.download_favourites().written == ()


def test_the_library_follows_its_setting_when_the_favourites_are_pulled_again(tmp_path: Path) -> None:
    """A **Library path** that changed, and the one action that makes the new folder whole.

    The old file is not moved and not deleted — wallpapi will not reach outside the folder it is pointed
    at — but everything that resolves to **Favourite** is written where the setting points now. That is
    the answer to "the **Library** is somewhere else today" that needs no folder-walking at all.
    """
    original = tmp_path / "Original"
    moved = tmp_path / "Moved"
    core, _ = core_with_a_real_writer(tmp_path, catalogue_size=2)
    core.update_settings(batch_size=2, library_path=original)
    batch = core.get_next_batch()
    assert isinstance(batch, Batch)
    core.set_all_draft_verdicts(batch.id, Verdict.FAVOURITE)
    core.submit_batch(batch.id)
    favourited = sorted(w.id for w in batch.wallpapers)

    core.update_settings(library_path=moved)
    pulled = core.download_favourites()

    assert pulled.skipped == ()
    assert sorted(pulled.written) == favourited
    assert sorted(p.name for p in moved.iterdir()) == [f"{i}.jpg" for i in favourited]
    assert sorted(p.name for p in original.iterdir()) == [f"{i}.jpg" for i in favourited]


def test_a_recorded_file_already_gone_is_dropped_without_being_looked_for(tmp_path: Path) -> None:
    """The maintainer's rule: if the file cannot be found, do not go looking for it.

    The user deleted it in Explorer and then took the **Favourite** back. Nothing goes looking for it —
    not under the old name, not anywhere else — and nothing is raised. The row goes, the **Decision log**
    keeps both entries, and a **Library** folder that was left empty stays empty.
    """
    library_path = tmp_path / "Library"
    core, seen = core_with_a_real_writer(tmp_path)
    core.update_settings(batch_size=1, library_path=library_path)
    batch = core.get_next_batch()
    assert isinstance(batch, Batch)
    shown = batch.wallpapers[0].id
    core.set_draft_verdict(batch.id, shown, Verdict.FAVOURITE)
    core.submit_batch(batch.id)
    (library_path / f"{shown}.jpg").unlink()

    assert core.edit_verdict(shown, Verdict.LIKE) is None

    assert list(library_path.iterdir()) == []
    assert len(seen) == 1
    assert [e.entry for e in core.list_history(batch_id=batch.id)] == [Verdict.FAVOURITE]
    assert core.resolve_verdicts([shown])[shown].verdict is Verdict.LIKE
    assert core.reconcile_library() == core.reconcile_library()
