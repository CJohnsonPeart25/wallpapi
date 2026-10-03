"""The **Library** on a real disk: the confinement guard, the file name, and the real writer.

The guard decides every write and deletion (invariant 9): the name must be a Wallhaven ID and one short
extension, and the resolved path must lie strictly inside the resolved **Library** folder. The writer is the
other half: atomic writes (invariant 10) and only regular files unlinked. Real bytes on `tmp_path`, over a
mocked transport.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx2
import pytest

from tests.conftest import LibraryRig, library_junction, library_rig, library_symlink
from tests.fakes import catalogue_of
from wallpapi.library import DownloadingLibraryWriter, confined_to_library, library_file_name
from wallpapi.model import Verdict

IMAGE_BYTES = b"\xff\xd8\xff\xe0 full resolution, allegedly"
FULL_URL = "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.jpg"


# -- the guard ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inside",
    [
        pytest.param(("wp0001.jpg",), id="a file in the folder"),
        pytest.param(("nested", "wp0001.jpg"), id="strictly inside, not one level down"),
    ],
)
def test_a_file_inside_the_library_is_confined_to_its_resolved_path(
    tmp_path: Path, inside: tuple[str, ...]
) -> None:
    """The resolved path comes back because it is the one to write: invariant 10's temp file is a sibling
    of whatever it is handed. The folder need not exist yet, since the first **Favourite** creates it."""
    library = tmp_path / "not" / "yet" / "Library"

    assert confined_to_library(library.joinpath(*inside), library) == library.joinpath(*inside).resolve()


@pytest.mark.skipif(os.name != "nt", reason="only Windows compares paths case-insensitively")
def test_windows_compares_the_resolved_paths_case_insensitively(tmp_path: Path) -> None:
    """The folder does not exist, which is when the case survives resolution: an existing Windows path
    comes back canonically cased, and a comparison that only worked then would work by accident."""
    library = tmp_path / "Library"

    assert confined_to_library(Path(str(library).upper()) / "wp0001.jpg", library) is not None


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("Library/../../Windows/System32/x.jpg", id="a traversal out"),
        pytest.param("Elsewhere/wp0001.jpg", id="elsewhere on the disk"),
        pytest.param("Library", id="the folder itself"),
        pytest.param("Library2/wp0001.jpg", id="a sibling with the folder as a name prefix"),
        pytest.param("Library/wp0001", id="no extension"),
        pytest.param("Library/wp0001.jpg.exe", id="two extensions"),
        pytest.param("Library/wp 0001.jpg", id="a space"),
        pytest.param("Library/.jpg", id="no ID"),
        pytest.param("Library/wp0001.JPG", id="an uppercase extension"),
        pytest.param("Library/wp0001.thisistoolong", id="a long extension"),
    ],
)
def test_a_path_that_is_not_a_library_file_is_refused(tmp_path: Path, relative: str) -> None:
    """Compared by resolved path component, never by string prefix, so `Library2` is not inside `Library`."""
    assert confined_to_library(tmp_path / relative, tmp_path / "Library") is None


def _symlinked_folder(link: Path, target: Path) -> None:
    library_symlink(link, target, directory=True)


LINKS: list[Callable[[Path, Path], None]] = [_symlinked_folder, library_junction]


@pytest.mark.parametrize("make_link", LINKS, ids=["symlink", "junction"])
def test_a_link_inside_the_library_leading_out_of_it_is_refused(
    tmp_path: Path, make_link: Callable[[Path, Path], None]
) -> None:
    """Inside the folder by every syntactic measure and outside it in fact: why the guard resolves."""
    library = tmp_path / "Library"
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    library.mkdir()
    make_link(library / "linked", outside)

    assert confined_to_library(library / "linked" / "wp0001.jpg", library) is None


@pytest.mark.parametrize("make_link", LINKS, ids=["symlink", "junction"])
def test_a_library_folder_reached_through_a_link_still_confines(
    tmp_path: Path, make_link: Callable[[Path, Path], None]
) -> None:
    real = tmp_path / "Real"
    real.mkdir()
    library = tmp_path / "Linked"
    make_link(library, real)

    assert confined_to_library(library / "wp0001.jpg", library) == (real / "wp0001.jpg").resolve()


# -- the name a Library file takes -------------------------------------------------------------------


def test_the_name_is_the_wallhaven_id_and_the_url_suffix() -> None:
    assert library_file_name("wp0001", "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.png") == "wp0001.png"


@pytest.mark.parametrize(
    "url",
    [
        "https://w.wallhaven.cc/full/wp/wallhaven-wp0001",
        "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.JPG",
        "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.this-is-not-a-suffix",
        "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.thisistoolong",
        "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.jpg?signed=../../../x",
        "not a url at all",
    ],
)
def test_a_suffix_that_does_not_fit_the_pattern_falls_back_to_jpg(url: str) -> None:
    """The suffix is Wallhaven's and the name is ours. A fallback, not a refusal: the bytes are a wallpaper
    whatever the URL calls itself, and Wallhaven serves JPEG for nearly all of them."""
    assert library_file_name("wp0001", url) == "wp0001.jpg"


@pytest.mark.parametrize("wallpaper_id", ["..", "../../etc", "wp 0001", "wp.0001", "", "wp/0001"])
def test_a_wallhaven_id_that_is_not_alphanumeric_names_nothing(wallpaper_id: str) -> None:
    assert library_file_name(wallpaper_id, "https://w.wallhaven.cc/full/x/y.jpg") is None


# -- the writer --------------------------------------------------------------------------------------


def downloading_writer(served: bytes = IMAGE_BYTES) -> tuple[DownloadingLibraryWriter, list[httpx2.Request]]:
    """The real writer over a transport that answers from memory, plus the requests it made."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=served)

    return DownloadingLibraryWriter(client=httpx2.Client(transport=httpx2.MockTransport(handler))), seen


def test_a_write_creates_the_folder_replaces_in_place_and_leaves_no_part_file(tmp_path: Path) -> None:
    """The temp file is a sibling inside the folder, since `os.replace` raises across drives on Windows,
    and it must not survive. `os.replace` overwrites, so a re-**Favourite** needs no delete first."""
    destination = tmp_path / "Library" / "nested" / "wp0001.png"
    stale, _ = downloading_writer(b"stale")
    stale.write("wp0001", "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.png", destination)
    fresh, seen = downloading_writer()

    written = fresh.write("wp0001", "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.png", destination)

    assert written == destination
    assert written.read_bytes() == IMAGE_BYTES
    assert [request.url.path for request in seen] == ["/full/wp/wallhaven-wp0001.png"]
    assert [p.name for p in destination.parent.iterdir()] == [destination.name]


def test_a_download_that_dies_part_way_leaves_nothing_behind(tmp_path: Path) -> None:
    """A **Favourite** with no file is retried by the next reconciliation; one with half a file would not
    be, so the failure must reach the workflow and neither file may survive."""
    destination = tmp_path / "Library" / "wp0001.jpg"

    def dies_part_way() -> Iterator[bytes]:
        yield b"the first half"
        raise httpx2.ReadError("the connection went away")

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, content=dies_part_way())

    writer = DownloadingLibraryWriter(client=httpx2.Client(transport=httpx2.MockTransport(handler)))

    with pytest.raises(httpx2.ReadError):
        writer.write("wp0001", FULL_URL, destination)

    assert list(destination.parent.iterdir()) == []


def test_removing_a_file_that_is_already_gone_is_a_no_op(tmp_path: Path) -> None:
    """Invariant 9: "**Library** file deleted in Explorer" is a reachable state."""
    writer, _ = downloading_writer()

    writer.remove(tmp_path / "Library" / "never-existed.jpg")

    assert not (tmp_path / "Library" / "never-existed.jpg").exists()


def test_a_directory_is_never_removed(tmp_path: Path) -> None:
    """Already confined, so this is inside the folder the user chose: the thing that must survive a wrong
    row."""
    writer, _ = downloading_writer()
    folder = tmp_path / "Library" / "wp0001.jpg"
    folder.mkdir(parents=True)
    (folder / "something-of-the-users.txt").write_bytes(b"theirs")

    writer.remove(folder)

    assert folder.is_dir()
    assert (folder / "something-of-the-users.txt").exists()


@pytest.mark.parametrize("make_link", LINKS, ids=["symlink", "junction"])
def test_a_link_is_never_removed(tmp_path: Path, make_link: Callable[[Path, Path], None]) -> None:
    """Deleting a link deletes a name the user made, and wallpapi made no links."""
    library = tmp_path / "Library"
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    (outside / "precious.jpg").write_bytes(b"not wallpapi's")
    library.mkdir()
    make_link(library / "wp0001.jpg", outside)
    writer, _ = downloading_writer()

    writer.remove(library / "wp0001.jpg")

    assert os.path.lexists(library / "wp0001.jpg")
    assert (outside / "precious.jpg").read_bytes() == b"not wallpapi's"


# -- the Library with the real writer ----------------------------------------------------------------


@contextmanager
def favourited(tmp_path: Path, count: int) -> Generator[tuple[LibraryRig, list[httpx2.Request], list[str]]]:
    """The **Library** with the real writer, `count` **Favourites** just reconciled onto the disk."""
    writer, seen = downloading_writer()
    wallpapers = catalogue_of(count)
    ids = [w.id for w in wallpapers]
    with library_rig(tmp_path, writer, wallpapers) as rig:
        rig.decide(Verdict.FAVOURITE, *ids)
        rig.reconcile()
        yield rig, seen, ids


def test_a_library_deleted_in_explorer_changes_nothing_until_one_download_brings_it_back(
    tmp_path: Path,
) -> None:
    """ "If I delete the file myself, or move devices and bring the db, do not remove it from the
    **Decision log**; I want all my **Favourites** back with a single download." Reconciling does not
    notice the deletion, because the **Library** is never read back; the download checks only the paths
    wallpapi recorded."""
    with favourited(tmp_path, 3) as (rig, seen, ids):
        library_path = rig.library_path
        assert sorted(p.name for p in library_path.iterdir()) == [f"{i}.jpg" for i in ids]

        for file in library_path.iterdir():
            file.unlink()
        reconciled = rig.reconcile()

        assert (reconciled.written, reconciled.removed, reconciled.failed) == ((), (), ())
        assert len(seen) == 3

        pulled = rig.download_favourites()

        assert sorted(pulled.written) == ids
        assert pulled.failed == ()
        assert sorted(p.name for p in library_path.iterdir()) == [f"{i}.jpg" for i in ids]
        assert all(p.read_bytes() == IMAGE_BYTES for p in library_path.iterdir())
        assert len(seen) == 6


def test_a_favourite_whose_file_is_still_there_is_skipped(tmp_path: Path) -> None:
    """Only what is missing is fetched, and the folder is never listed: a file the user put there by hand
    is neither noticed nor counted."""
    with favourited(tmp_path, 2) as (rig, seen, (kept, deleted)):
        (rig.library_path / f"{deleted}.jpg").unlink()
        (rig.library_path / "a-photo-of-the-users.jpg").write_bytes(b"theirs")

        pulled = rig.download_favourites()

        assert pulled.written == (deleted,)
        assert pulled.skipped == (kept,)
        assert len(seen) == 3
        assert (rig.library_path / "a-photo-of-the-users.jpg").read_bytes() == b"theirs"
        assert rig.download_favourites().written == ()


def test_the_library_follows_its_setting_when_the_favourites_are_pulled_again(tmp_path: Path) -> None:
    """The old files are neither moved nor deleted, since wallpapi will not reach outside the folder it is
    pointed at; everything **Favourite** is written where the setting points now."""
    with favourited(tmp_path, 2) as (rig, _, ids):
        original = rig.library_path
        rig.library_path = tmp_path / "Moved"

        pulled = rig.download_favourites()

        assert pulled.skipped == ()
        assert sorted(pulled.written) == ids
        assert sorted(p.name for p in rig.library_path.iterdir()) == [f"{i}.jpg" for i in ids]
        assert sorted(p.name for p in original.iterdir()) == [f"{i}.jpg" for i in ids]


def test_a_recorded_path_outside_the_library_is_dropped_from_the_record_and_left_on_disk(
    tmp_path: Path,
) -> None:
    """Invariant 9. The file is untouched where it is; the record is gone, because **Favouriting** it again
    writes a new file where the setting points today, which a surviving row would have stopped."""
    with favourited(tmp_path, 1) as (rig, seen, (shown,)):
        abandoned = rig.library_path / f"{shown}.jpg"
        rig.library_path = tmp_path / "Moved"

        rig.decide(Verdict.LIKE, shown)
        reconciled = rig.reconcile()

        assert (reconciled.written, reconciled.removed, reconciled.failed) == ((), (), ())
        assert abandoned.read_bytes() == IMAGE_BYTES

        rig.decide(Verdict.FAVOURITE, shown)

        assert rig.reconcile().written == (shown,)
        assert (rig.library_path / f"{shown}.jpg").read_bytes() == IMAGE_BYTES
        assert abandoned.read_bytes() == IMAGE_BYTES
        assert len(seen) == 2


def test_a_recorded_file_already_gone_is_dropped_without_being_looked_for(tmp_path: Path) -> None:
    """Deleted in Explorer, then the **Favourite** taken back: nothing goes looking for it and nothing is
    raised. The row goes."""
    with favourited(tmp_path, 1) as (rig, seen, (shown,)):
        (rig.library_path / f"{shown}.jpg").unlink()

        rig.decide(Verdict.LIKE, shown)

        assert rig.reconcile().removed == (shown,)
        assert list(rig.library_path.iterdir()) == []
        assert len(seen) == 1
        assert rig.reconcile() == rig.reconcile()
