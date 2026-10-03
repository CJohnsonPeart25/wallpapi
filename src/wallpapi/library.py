"""The **Library** writer seam, and the writer behind it.

Nothing is ever read back out of the **Library**, and the writer has no "does this exist" method. The one
question ever asked of the folder, whether a recorded path is still there, is the **Favourite** download's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx2

from wallpapi.files import write_atomically

REQUEST_TIMEOUT = 10.0
"""Seconds. Below the shutdown join timeout, so shutdown cannot hang mid-download (invariant 12)."""


class LibraryWriter(Protocol):
    """What the Core service needs of the **Library**."""

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        """Download `source_url` to `destination`, returning the absolute path actually written."""
        ...

    def remove(self, path: Path) -> None:
        """Delete an already-confined recorded path if it is a regular file, tolerating it being gone."""
        ...


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
