"""The **Library**: reconciling it with the **Decision log**, the **Favourite** download, the confinement
guard every write and deletion passes, and the writer behind them. Nothing else reads or writes
`library_files`.

Nothing is ever read back out of the **Library**, and the writer has no "does this exist" method. The one
question ever asked of the folder, whether a recorded path is still there, is the **Favourite** download's.
Both operations run after the **Decision log** has committed and take a connection, not a write handle: each
file is downloaded first and recorded in a short transaction of its own, so no network call is ever made
inside a write (ADR 0006).
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx2

from wallpapi import decisions, storage
from wallpapi.clock import Clock
from wallpapi.files import url_suffix, write_atomically

REQUEST_TIMEOUT = 10.0
"""Seconds. Below the shutdown join timeout, so shutdown cannot hang mid-download (invariant 12)."""

LIBRARY_FILE_NAME = re.compile(r"[A-Za-z0-9]+\.[a-z0-9]{1,5}")
"""The only shape a **Library** file name may take: a Wallhaven ID, a dot, one short extension.

A name is never a path: `..`, separators, spaces and a second dot are all refused. ASCII by construction,
because `str.isalnum` counts `²`.
"""

DEFAULT_LIBRARY_SUFFIX = ".jpg"
"""What a `full_url` with no recognisable extension is saved as: Wallhaven serves JPEG nearly always."""


@dataclass(frozen=True, slots=True)
class LibraryReconciliation:
    """What one **Library** reconciliation did. It runs after commit and cannot raise, so `failed` is the
    report.
    """

    written: tuple[str, ...]
    removed: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FavouriteDownload:
    """What one "download all **Favourites**" pass did. It never deletes; the settings page says all three."""

    written: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]


class LibraryWriter(Protocol):
    """What the **Library** needs of the disk."""

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        """Download `source_url` to `destination`, returning the absolute path actually written."""
        ...

    def remove(self, path: Path) -> None:
        """Delete an already-confined recorded path if it is a regular file, tolerating it being gone."""
        ...


class Library:
    """Reconciliation and the **Favourite** download over the `library_files` record, with the writer and the
    clock that stamps each record.
    """

    def __init__(self, writer: LibraryWriter, clock: Clock) -> None:
        self._writer = writer
        self._clock = clock

    def reconcile(self, connection: sqlite3.Connection, library_path: Path) -> LibraryReconciliation:
        """Make the **Library** folder agree with the **Decision log**, and report what that took.

        Idempotent and derived: a **Favourite** with no recorded file gets one, a recorded file whose
        **Wallpaper** is no longer a **Favourite** is deleted. Failures are collected rather than raised,
        because this runs after the **Decision log** has committed. Nothing here stats a path.
        """
        favourites, rows = _candidates(connection)

        written: list[str] = []
        removed: list[str] = []
        failed: list[str] = []
        for row in rows:
            wallpaper_id = str(row["wallpaper_id"])
            recorded = None if row["path"] is None else Path(str(row["path"]))
            wanted = wallpaper_id in favourites
            try:
                if wanted and recorded is None:
                    if self._add(connection, wallpaper_id, str(row["full_url"]), library_path):
                        written.append(wallpaper_id)
                    else:
                        failed.append(wallpaper_id)
                elif not wanted and recorded is not None:
                    deleted = self._drop(connection, wallpaper_id, recorded, library_path)
                    # A row outside the **Library** is dropped with its file left where it is.
                    if deleted:
                        removed.append(wallpaper_id)
            except Exception:
                # The writer declares no error type. Leaving the row as it was makes the next call retry.
                failed.append(wallpaper_id)
        return LibraryReconciliation(written=tuple(written), removed=tuple(removed), failed=tuple(failed))

    def download_favourites(self, connection: sqlite3.Connection, library_path: Path) -> FavouriteDownload:
        """Write a **Library** file for every **Favourite** that has not got one. Never deletes.

        The one place wallpapi asks the folder anything: whether a recorded path is still there. A
        **Favourite** counts as missing its file with no record, a recorded path not on the disk, or one no
        longer confined; it is written into the **Library path** of today and the record replaced.
        """
        favourites, rows = _candidates(connection)

        written: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for row in rows:
            wallpaper_id = str(row["wallpaper_id"])
            if wallpaper_id not in favourites:
                continue
            recorded = None if row["path"] is None else Path(str(row["path"]))
            # The guard first, so the only paths ever stat-ed are inside the **Library** folder.
            held = None if recorded is None else confined_to_library(recorded, library_path)
            if held is not None and held.exists():
                skipped.append(wallpaper_id)
                continue
            try:
                if self._add(connection, wallpaper_id, str(row["full_url"]), library_path):
                    written.append(wallpaper_id)
                else:
                    failed.append(wallpaper_id)
            except Exception:
                # As in `reconcile`: the writer declares no error type.
                failed.append(wallpaper_id)
        return FavouriteDownload(written=tuple(written), skipped=tuple(skipped), failed=tuple(failed))

    def _add(
        self, connection: sqlite3.Connection, wallpaper_id: str, source_url: str, library_path: Path
    ) -> bool:
        """Download one **Favourite** and record where it landed, or return `False` before fetching anything.

        The confinement check comes first, so a refusal never costs a download. `written_at` is the clock's
        moment as an ISO 8601 UTC string (invariant 5); a naive one is refused.
        """
        name = library_file_name(wallpaper_id, source_url)
        if name is None:
            return False
        destination = confined_to_library(library_path / name, library_path)
        if destination is None:
            return False
        written = self._writer.write(wallpaper_id, source_url, destination)
        at = self._clock.now()
        if at.tzinfo is None:
            raise ValueError("a Library timestamp must be UTC-aware")
        with storage.write(connection) as write:
            write.execute(
                _RECORD_LIBRARY_FILE, (wallpaper_id, str(written), at.astimezone(dt.UTC).isoformat())
            )
        return True

    def _drop(
        self, connection: sqlite3.Connection, wallpaper_id: str, recorded: Path, library_path: Path
    ) -> bool:
        """Forget a recorded **Library** file, deleting it only if it is still confined (invariant 9).

        The row goes either way, so a refusal happens once rather than on every submission. Returns whether
        the file was handed to the writer; a failed deletion raises with the row intact, to be retried.
        """
        confined = confined_to_library(recorded, library_path)
        if confined is not None:
            self._writer.remove(confined)
        with storage.write(connection) as write:
            write.execute("DELETE FROM library_files WHERE wallpaper_id = ?", (wallpaper_id,))
        return confined is not None


def library_file_name(wallpaper_id: str, source_url: str) -> str | None:
    """What a **Library** file is called, or `None` if this **Wallpaper** cannot have one.

    A bad suffix falls back to `.jpg`; a bad ID names nothing. The suffix is Wallhaven's to get wrong, and the
    ID is what the file is for.
    """
    named = f"{wallpaper_id}{url_suffix(source_url, default=DEFAULT_LIBRARY_SUFFIX)}"
    if LIBRARY_FILE_NAME.fullmatch(named):
        return named
    fallback = f"{wallpaper_id}{DEFAULT_LIBRARY_SUFFIX}"
    return fallback if LIBRARY_FILE_NAME.fullmatch(fallback) else None


def confined_to_library(path: Path, library_root: Path) -> Path | None:
    """The resolved `path`, if wallpapi may write or delete it — otherwise `None`. The one guard for both.

    The name fits `LIBRARY_FILE_NAME`, and the path, fully resolved (`..` collapsed, every symlink and
    junction followed), lies strictly inside the fully resolved **Library** folder. Resolving is the point: a
    link in the folder is inside it syntactically and outside it in fact. Compared through `os.path.normcase`
    and by component, because NTFS ignores case and a string prefix would put `Library2` inside `Library`.

    The resolved path is returned because it is the one the caller must use: the atomic write's temp file is a
    sibling of whatever it is handed. `strict=False` because the folder need not exist yet.
    """
    try:
        resolved = path.resolve(strict=False)
        root = library_root.resolve(strict=False)
    except OSError, ValueError:
        # A hand-edited row can hold characters Windows will not even parse. Unresolvable is refused.
        return None
    if not LIBRARY_FILE_NAME.fullmatch(resolved.name):
        return None
    here = os.path.normcase(str(resolved))
    there = os.path.normcase(str(root))
    if here == there:
        return None
    try:
        if os.path.commonpath((here, there)) != there:
            return None
    except ValueError:
        # Different drives, or one of the two not absolute. Either way there is no "inside" to be in.
        return None
    return resolved


class DownloadingLibraryWriter:
    """The real writer: full-resolution bytes streamed from `w.wallhaven.cc` onto the disk, atomically.

    `w.wallhaven.cc` is not the API host, so these downloads are not **API calls**.
    """

    def __init__(self, client: httpx2.Client | None = None) -> None:
        self._client = httpx2.Client(timeout=REQUEST_TIMEOUT) if client is None else client

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        """Download to `destination`, creating the **Library** folder if this is its first file.

        `wallpaper_id` is unused here; it is in the protocol for fakes and for a writer that names its files
        differently. The path returned is the one recorded.
        """
        del wallpaper_id
        with self._client.stream("GET", source_url) as response:
            response.raise_for_status()
            write_atomically(destination, response.iter_bytes())
        return destination

    def remove(self, path: Path) -> None:
        """Delete a recorded path if it is a regular file, tolerating it already being gone.

        `is_symlink` first, because `is_file` follows the link; a Windows junction is a directory to Python,
        so `is_file` turns that one away. `missing_ok` as well, because the check races with Explorer.
        """
        if path.is_symlink() or not path.is_file():
            return
        path.unlink(missing_ok=True)

    def close(self) -> None:
        self._client.close()


def _candidates(connection: sqlite3.Connection) -> tuple[set[str], list[sqlite3.Row]]:
    """The **Favourites**, and every row a reconciliation might act on: a **Favourite**, or one holding a
    recorded file.
    """
    favourites = decisions.favourites(connection)
    # SQLite's parameter limit is 32,766 here, well above any count of **Favourites**.
    placeholders = ",".join("?" * len(favourites))
    rows = connection.execute(_LIBRARY_CANDIDATES.format(favourites=placeholders), favourites).fetchall()
    return set(favourites), rows


_LIBRARY_CANDIDATES = """
SELECT w.id AS wallpaper_id, w.full_url AS full_url, f.path AS path
FROM wallpapers AS w
LEFT JOIN library_files AS f ON f.wallpaper_id = w.id
WHERE f.wallpaper_id IS NOT NULL OR w.id IN ({favourites})
ORDER BY w.id
"""
"""Everything a reconciliation might act on: a **Favourite** now, or holding a recorded file.

`{favourites}` is placeholders built here; the ids from `decisions.favourites` are bound as parameters.
"""

_RECORD_LIBRARY_FILE = """
INSERT INTO library_files (wallpaper_id, path, written_at) VALUES (?, ?, ?)
ON CONFLICT (wallpaper_id) DO UPDATE SET path = excluded.path, written_at = excluded.written_at
"""
"""An upsert, so an unexpected existing row is overwritten rather than an `IntegrityError` retried for
ever.
"""
