"""Putting a file on disk without ever leaving a half-written one there; shared by the **Thumbnail cache** and
the **Library**.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4


def write_atomically(destination: Path, chunks: bytes | Iterable[bytes]) -> None:
    """Write via a temp file beside `destination`, then `os.replace` it into place.

    The temp file is a sibling because `os.replace` is atomic only within one filesystem and raises across
    drives on Windows. The parent folder is created on first write.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{uuid4().hex}.part")
    try:
        with temporary.open("wb") as handle:
            for chunk in (chunks,) if isinstance(chunks, bytes) else chunks:
                handle.write(chunk)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def url_suffix(url: str, *, default: str = ".jpg") -> str:
    """The file extension of a URL's path, ignoring any query string."""
    return PurePosixPath(urlsplit(url).path).suffix or default
