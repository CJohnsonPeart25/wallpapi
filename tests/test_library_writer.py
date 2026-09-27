"""The real **Library** writer, against a temporary folder and a mocked transport rather than the network.

Two things only this writer can be asked: that the file arrives whole (invariant 10 — temp file inside the
**Library** folder, then `os.replace`), and that deleting a **Library** file in Explorer changes nothing.
The last one needs a writer that puts real bytes on a real disk, so it is here rather than with the fake.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from tests.conftest import FIXED_NOW
from tests.fakes import FakeClock, FakeSimilarityProvider, FakeWallhavenClient, catalogue_of
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
    writer, seen = downloading_writer()
    core = CoreService(
        db_path=tmp_path / "wallpapi.db",
        wallhaven=FakeWallhavenClient(catalogue_of(1)),
        library=writer,
        similarity=FakeSimilarityProvider(),
        random_source=SeededRandom(1),
        clock=FakeClock(FIXED_NOW),
    )
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
