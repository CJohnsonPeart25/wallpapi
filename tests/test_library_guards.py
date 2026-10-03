"""The guard that keeps every **Library** write and deletion inside the **Library** folder. Issue #14.

Deleting a file wallpapi wrote is acceptable only under guarantees that make a wrong deletion impossible.
Two of them, and one function decides both: the name must be a Wallhaven ID and one dotted extension, and
the path, fully resolved, must lie strictly inside the resolved **Library** folder. A recorded path that
fails either is never touched — the `library_files` row goes and the file stays exactly where it is.

Tested three ways, because a guard is worth no more than the weakest of them: directly, as the pure
function it is; through the Core service with the fake writer, where the refusal has to be a behaviour and
not a log line; and against `tmp_path` with a real symlink, which is the case only a real filesystem poses.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import Harness, favourite_the_whole_batch, make_harness
from tests.fakes import catalogue_of, wallpaper
from wallpapi.core import confined_to_library, library_file_name
from wallpapi.model import Verdict


def library_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    """Make `link` a symlink to `target`, or skip — Windows needs a privilege tests cannot assume."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as unavailable:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks are not available here: {unavailable}")


def library_junction(link: Path, target: Path) -> None:
    """Make `link` a Windows directory junction to `target`, or skip.

    The symlink tests above skip on an ordinary Windows account — `CreateSymbolicLink` needs a privilege
    a developer machine does not hand out by default — and this is what stops the most important case in
    the ticket from going untested on the one platform wallpapi runs on. A junction is a reparse point
    just the same, it needs no privilege at all, and `Path.resolve` follows it, which is the whole of what
    the guard depends on.
    """
    if os.name != "nt":  # pragma: no cover - platform dependent
        pytest.skip("junctions are a Windows thing; the symlink tests carry this elsewhere")
    link.parent.mkdir(parents=True, exist_ok=True)
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
    )
    if made.returncode != 0:  # pragma: no cover - platform dependent
        pytest.skip(f"junctions are not available here: {made.stderr.decode(errors='replace').strip()}")


# -- the guard, directly ---------------------------------------------------------------------------------


def test_a_file_in_the_library_folder_is_confined(tmp_path: Path) -> None:
    """The ordinary case, and the resolved path is what comes back — that is what gets written.

    Returning the resolved path rather than a yes or no is the point: the temp file and the `os.replace`
    behind every **Library** write are siblings of whatever path they are given (invariant 10), so the
    path that was checked has to be the path that is used or the check has a gap under it.
    """
    library = tmp_path / "Library"

    confined = confined_to_library(library / "wp0001.jpg", library)

    assert confined == (library / "wp0001.jpg").resolve()


def test_a_folder_that_does_not_exist_yet_still_confines(tmp_path: Path) -> None:
    """#4 stores the **Library path** without touching the filesystem, so the folder may well not be there.

    `Path.resolve(strict=False)` is what makes that a non-event: the first **Favourite** is what creates
    the folder, and a guard that demanded it already existed would refuse every first write.
    """
    library = tmp_path / "not" / "yet" / "Library"

    assert confined_to_library(library / "wp0001.jpg", library) is not None


def test_a_subfolder_of_the_library_is_confined(tmp_path: Path) -> None:
    """Strictly inside, not strictly one level down. wallpapi writes flat; a recorded row need not be."""
    library = tmp_path / "Library"

    assert confined_to_library(library / "nested" / "wp0001.jpg", library) is not None


def test_a_traversal_out_of_the_library_is_refused(tmp_path: Path) -> None:
    """The maintainer's case, spelled the way they spelled it.

    `..` is resolved before anything is compared, so a name that climbs out of the folder is simply a path
    outside the folder and is refused as one — nothing has to recognise the `..` itself.
    """
    library = tmp_path / "Library"

    assert confined_to_library(library / ".." / ".." / "Windows" / "System32" / "x.jpg", library) is None


def test_an_absolute_path_elsewhere_on_the_disk_is_refused(tmp_path: Path) -> None:
    """A row that names somewhere else entirely — a hand-edited database, or a **Library path** change."""
    library = tmp_path / "Library"

    assert confined_to_library(tmp_path / "Elsewhere" / "wp0001.jpg", library) is None


def test_the_library_folder_itself_is_refused(tmp_path: Path) -> None:
    """Strictly inside. The folder is not a file in it, and deleting it is exactly the accident to stop."""
    library = tmp_path / "Library"

    assert confined_to_library(library, library) is None


def test_a_sibling_folder_with_the_library_as_a_name_prefix_is_refused(tmp_path: Path) -> None:
    """`Library2` is not inside `Library`, however much its name looks like it.

    Compared by path component and not by string prefix, which is the difference between this passing and
    a `startswith` that would happily delete out of the wrong folder.
    """
    assert confined_to_library(tmp_path / "Library2" / "wp0001.jpg", tmp_path / "Library") is None


def test_a_symlink_inside_the_library_pointing_out_of_it_is_refused(tmp_path: Path) -> None:
    """The case that makes resolution non-negotiable.

    The path is inside the **Library** folder by every syntactic measure. Only following the link says
    otherwise, which is why the guard resolves rather than normalising.
    """
    library = tmp_path / "Library"
    outside = tmp_path / "Elsewhere" / "precious.jpg"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not wallpapi's")
    library_symlink(library / "wp0001.jpg", outside)

    assert confined_to_library(library / "wp0001.jpg", library) is None


def test_a_junction_inside_the_library_leading_out_of_it_is_refused(tmp_path: Path) -> None:
    """The same case as the symlink above, in the form Windows will actually make without asking.

    `Library\\linked\\wp0001.jpg` is inside the **Library** folder to look at and in `Elsewhere` in fact.
    Nothing in the path says so — no `..`, no drive letter, no second root — which is precisely why the
    guard resolves rather than inspecting.
    """
    library = tmp_path / "Library"
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    library.mkdir()
    library_junction(library / "linked", outside)

    assert confined_to_library(library / "linked" / "wp0001.jpg", library) is None


def test_a_library_folder_reached_through_a_junction_still_confines(tmp_path: Path) -> None:
    """And the other way round: the **Library path** itself may be a reparse point without being refused."""
    real = tmp_path / "Real"
    real.mkdir()
    library = tmp_path / "Linked"
    library_junction(library, real)

    assert confined_to_library(library / "wp0001.jpg", library) == (real / "wp0001.jpg").resolve()


def test_a_library_folder_reached_through_a_symlink_still_confines(tmp_path: Path) -> None:
    """Both sides resolve, so a **Library path** that goes through a link is not refused for doing so."""
    real = tmp_path / "Real"
    real.mkdir()
    library = tmp_path / "Linked"
    library_symlink(library, real, directory=True)
    (library / "wp0001.jpg").write_bytes(b"wallpapi's")

    assert confined_to_library(library / "wp0001.jpg", library) == (real / "wp0001.jpg").resolve()


@pytest.mark.skipif(os.name != "nt", reason="only Windows compares paths case-insensitively")
def test_windows_compares_the_resolved_paths_case_insensitively(tmp_path: Path) -> None:
    """A recorded path differing only in case names the same file here, and is confined.

    The folder deliberately does not exist yet, because that is when the case survives resolution: a
    Windows path that is already there comes back canonically cased whatever was asked for, and a
    comparison that only worked for folders already on disk would be a comparison that worked by accident.
    """
    library = tmp_path / "Library"

    assert confined_to_library(Path(str(library).upper()) / "wp0001.jpg", library) is not None


@pytest.mark.parametrize(
    "name",
    [
        "wp0001",  # no extension at all
        "wp0001.jpg.exe",  # two, and the last is not an image
        "wp 0001.jpg",  # a space
        ".jpg",  # an extension and no ID
        "wp0001.JPG",  # the pattern is lowercase; a URL like that gets the fallback instead
        "wp0001.thisistoolong",
    ],
)
def test_a_name_that_is_not_an_id_and_one_extension_is_refused(tmp_path: Path, name: str) -> None:
    """The strict pattern, from the other side. Nothing wallpapi writes could be named any of these."""
    library = tmp_path / "Library"

    assert confined_to_library(library / name, library) is None


# -- the name a Library file takes -----------------------------------------------------------------------


def test_the_name_is_the_wallhaven_id_and_the_url_suffix() -> None:
    """Unchanged from #5: what the **Library** holds is readable as the **Wallpaper** it came from."""
    assert library_file_name("wp0001", "https://w.wallhaven.cc/full/wp/wallhaven-wp0001.png") == (
        "wp0001.png"
    )


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
    """`full_url` comes from Wallhaven, so the suffix is theirs rather than ours.

    A fallback and not a refusal: the bytes are a wallpaper whatever the URL calls itself, and Wallhaven
    serves JPEG for all but a handful. The name is ours to choose, which is the whole point of choosing
    one that cannot be anything but a file name.
    """
    assert library_file_name("wp0001", url) == "wp0001.jpg"


@pytest.mark.parametrize("wallpaper_id", ["..", "../../etc", "wp 0001", "wp.0001", "", "wp/0001"])
def test_a_wallhaven_id_that_is_not_alphanumeric_names_nothing(wallpaper_id: str) -> None:
    """No **Wallpaper** Wallhaven serves is called any of these, and none of them may become a path."""
    assert library_file_name(wallpaper_id, "https://w.wallhaven.cc/full/x/y.jpg") is None


# -- the guard through the Core service ------------------------------------------------------------------


def recorded_destination(harness: Harness, wallpaper_id: str) -> Path:
    """Where the **Library** file of one **Wallpaper** was written."""
    return next(w.destination for w in harness.library.written if w.wallpaper_id == wallpaper_id)


def test_a_recorded_path_outside_the_library_is_dropped_rather_than_deleted(
    db_path: Path, tmp_path: Path
) -> None:
    """The **Library path** changed, so the old file is no longer wallpapi's to delete.

    This reverses half of what #5 did, deliberately. Invariant 9 said the recorded path is deleted
    wherever it is; #14 says it is deleted only where wallpapi can still guarantee it is looking inside
    the folder the user chose. The row goes either way — as far as wallpapi is concerned the **Wallpaper**
    holds no **Library** file — and the file is left where it is for the user to deal with.
    """
    original = tmp_path / "Original"
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=original)
    first = favourite_the_whole_batch(harness)
    shown = first.wallpapers[0].id
    assert recorded_destination(harness, shown).parent == original.resolve()

    harness.core.update_settings(library_path=tmp_path / "Moved")
    assert harness.core.edit_verdict(shown, Verdict.LIKE) is None

    assert harness.library.removed == []


def test_the_row_of_a_refused_deletion_is_dropped(db_path: Path, tmp_path: Path) -> None:
    """Dropped, not kept: a refusal that left the row would be refused again on every submission.

    Observed the way the **Library** observes everything — by what happens next. The **Wallpaper** is
    **Favourited** again, and because it now holds no recorded file it is written afresh, into the folder
    the setting names today rather than the one it came from.
    """
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Original")
    first = favourite_the_whole_batch(harness)
    shown = first.wallpapers[0].id
    moved = tmp_path / "Moved"
    harness.core.update_settings(library_path=moved)
    assert harness.core.edit_verdict(shown, Verdict.LIKE) is None

    assert harness.core.edit_verdict(shown, Verdict.FAVOURITE) is None

    assert [w.wallpaper_id for w in harness.library.written] == [shown, shown]
    assert recorded_destination(harness, shown).parent == (tmp_path / "Original").resolve()
    assert harness.library.written[-1].destination == (moved / f"{shown}.jpg").resolve()
    assert harness.library.removed == []


def test_a_refused_deletion_settles_rather_than_repeating(db_path: Path, tmp_path: Path) -> None:
    """Reconciling again finds nothing to refuse, because there is no row left to refuse against."""
    harness = make_harness(db_path, catalogue=catalogue_of(1))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Original")
    shown = favourite_the_whole_batch(harness).wallpapers[0].id
    harness.core.update_settings(library_path=tmp_path / "Moved")
    assert harness.core.edit_verdict(shown, Verdict.LIKE) is None

    first = harness.core.reconcile_library()
    second = harness.core.reconcile_library()

    assert first == second
    assert first.written == ()
    assert first.removed == ()
    assert first.failed == ()
    assert harness.library.removed == []


def test_a_wallhaven_id_that_cannot_name_a_file_is_a_failure_and_no_download(
    db_path: Path, tmp_path: Path
) -> None:
    """Refused at write time, before any download — the other half of the guard, through the seam.

    Wallhaven does not serve IDs like this one. A **Pool** that has somehow acquired one must not be able
    to turn it into a path, and finding that out after the bytes have arrived would be finding it out too
    late. The **Favourite** stands and is reported as a failure: it is a fact about taste, and taste does
    not depend on wallpapi being able to name a file.
    """
    harness = make_harness(db_path, catalogue=(wallpaper("wp/0001"),))
    harness.core.update_settings(batch_size=1, library_path=tmp_path / "Library")

    favourite_the_whole_batch(harness)
    pulled = harness.core.download_favourites()

    assert harness.library.written == []
    assert pulled.written == ()
    assert pulled.failed == ("wp/0001",)
    assert harness.core.reconcile_library().failed == ("wp/0001",)
