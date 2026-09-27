"""The **Library** writer seam, and the writer behind it.

The **Library** is write-only: **Favourites** go in, nothing is ever read back. That is what makes deleting
a file in Explorer a state wallpapi cannot and should not notice, and it is why the writer has no "does this
exist" method — the Core service answers that from the path it recorded (invariant 9), never from the disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx2

from wallpapi.files import write_atomically

REQUEST_TIMEOUT = 10.0
"""Seconds, matching the Wallhaven client's. Must stay below the shutdown join timeout, so shutdown cannot
hang mid-download (invariant 12)."""


class LibraryWriter(Protocol):
    """What the Core service needs of the **Library**."""

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        """Download `source_url` to `destination`, returning the absolute path actually written."""
        ...

    def remove(self, path: Path) -> None:
        """Delete a path wallpapi itself recorded, if it is a regular file.

        Tolerates the file already being gone, and declines anything that is not a plain file — a
        directory, a junction or a symlink is not something wallpapi wrote, whatever a row says (#14).
        The Core service has already confined the path to the **Library** folder; this is the half of the
        guarantee that only the filesystem can answer.
        """
        ...


class DownloadingLibraryWriter:
    """The real writer: full-resolution bytes from `w.wallhaven.cc` onto the disk, atomically.

    Not an **API call** (invariant 11). Full-resolution images come from `w.wallhaven.cc`, a separate host
    from `wallhaven.cc/api`, so these must not be counted against the documented 45-per-minute limit. They
    do want throttling of their own, which belongs with the **Pool** refill at #6; here a **Favourite** is
    one download and the **Library** is only written when the **Decision log** says it is out of date, so
    the rate is a handful per **Batch** at worst.

    Streamed to disk rather than held in memory: a 5120x2880 wallpaper is megabytes, and there is nothing
    to do with the bytes but write them.
    """

    def __init__(self, client: httpx2.Client | None = None) -> None:
        self._client = httpx2.Client(timeout=REQUEST_TIMEOUT) if client is None else client

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        """Download to `destination`, creating the **Library** folder if this is its first file.

        `wallpaper_id` is not used here. It stays in the protocol because it is what the Core service keys
        the recorded path by, and because a fake — or a later writer that names its files differently —
        needs it. The path returned is what gets recorded, so such a writer would still be deleted from
        correctly.
        """
        del wallpaper_id
        with self._client.stream("GET", source_url) as response:
            response.raise_for_status()
            write_atomically(destination, response.iter_bytes())
        return destination

    def remove(self, path: Path) -> None:
        """Delete a recorded path if it is a regular file, tolerating it already being gone.

        Only regular files (#14). `is_symlink` first and on its own, because `is_file` follows the link
        and would answer for whatever is on the far end of it; a Windows junction is not a symlink to
        Python but is a directory, so `is_file` is what turns that one away. Neither is something wallpapi
        wrote, and unlinking one would be deleting a name the user made for something of their own.

        `missing_ok` as well as the check, not instead of it: "**Library** file deleted in Explorer" is a
        state the spec guarantees is reachable, and the stat above still races with the user's file
        manager however carefully it is written.
        """
        if path.is_symlink() or not path.is_file():
            return
        path.unlink(missing_ok=True)

    def close(self) -> None:
        self._client.close()
