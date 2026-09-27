"""Putting a file on disk without ever leaving a half-written one there.

Two callers want this and they want it for the same reason: the **Thumbnail cache** must never serve a
truncated image, and the **Library** must never hand Windows' slideshow one. It lives here rather than in
either of them because the **Library** and the **Thumbnail cache** are deliberately separate concerns
(invariant 8) and neither should have to import the other.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4


def write_atomically(destination: Path, chunks: bytes | Iterable[bytes]) -> None:
    """Write via a temp file beside `destination`, then `os.replace` it into place.

    The temp file must be a sibling (invariant 10): `os.replace` is only atomic within one filesystem and
    raises across drives on Windows, and both destinations here are user-configurable paths that can easily
    be on another drive from `%TEMP%`. A failure part way through leaves `destination` exactly as it was,
    whether that is absent or an older version of the same file, and takes the temp file with it.

    The parent directory is created if it is missing, which is how the **Library** folder comes into
    existence: #4 stores the path deliberately without touching the filesystem, so the first **Favourite**
    is what makes the folder.

    Chunks as well as one `bytes`, because a full-resolution wallpaper is megabytes and a streamed download
    has no reason to be assembled in memory first.
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
