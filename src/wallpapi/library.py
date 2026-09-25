"""The **Library** writer seam.

The **Library** is write-only: **Favourites** go in, nothing is ever read back. Nothing at #2 writes to it —
**Ignores** download nothing — so only the seam is defined here. The atomic write (invariant 10) and the
recorded absolute path (invariant 9) arrive with the writer itself at #5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class LibraryWriter(Protocol):
    """What the Core service needs of the **Library**."""

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        """Download `source_url` to `destination`, returning the absolute path actually written."""
        ...

    def remove(self, path: Path) -> None:
        """Delete a path wallpapi itself recorded. Tolerates the file already being gone."""
        ...


class UnbuiltLibraryWriter:
    """Stands in until the real writer arrives at #5.

    Unreachable at #2: **Favourites** need a **Draft Batch**, which is #3, so every submitted **Batch** is
    all **Ignores**. It raises rather than quietly doing nothing, because a **Favourite** that silently
    fails to download is worse than a crash.
    """

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        raise NotImplementedError("the Library writer arrives at #5")

    def remove(self, path: Path) -> None:
        raise NotImplementedError("the Library writer arrives at #5")
