"""The entry point: `uv run python -m wallpapi`.

This is the one place that wires the real dependencies together. Everything downstream of `build_core` is
already covered by tests against fakes, so this module stays thin enough to read in one go.

Bound to `127.0.0.1` and single-worker, both deliberate: `--workers N` would mean N writers against one
SQLite file, and later N background refill threads.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import uvicorn

from wallpapi.clock import SystemClock
from wallpapi.core import CoreService
from wallpapi.library import UnbuiltLibraryWriter
from wallpapi.rng import SeededRandom
from wallpapi.similarity import UnbuiltSimilarityProvider
from wallpapi.wallhaven import WallhavenClient
from wallpapi.web.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def wallpapi_home() -> Path:
    """Where the **Decision log** and the **Thumbnail cache** live."""
    configured = os.environ.get("WALLPAPI_HOME")
    return Path(configured) if configured else Path.home() / ".wallpapi"


def build_core(home: Path | None = None) -> CoreService:
    """The real Core service.

    The seed is fresh per boot unless `WALLPAPI_SEED` pins it — the random source is seedable so that tests
    are reproducible, not so that every run shows the same **Wallpapers**.
    """
    root = wallpapi_home() if home is None else home
    root.mkdir(parents=True, exist_ok=True)
    pinned = os.environ.get("WALLPAPI_SEED")
    return CoreService(
        db_path=root / "wallpapi.db",
        wallhaven=WallhavenClient(),
        library=UnbuiltLibraryWriter(),
        similarity=UnbuiltSimilarityProvider(),
        random_source=SeededRandom(int(pinned) if pinned else secrets.randbits(64)),
        clock=SystemClock(),
    )


def main() -> None:
    port = int(os.environ.get("WALLPAPI_PORT", DEFAULT_PORT))
    uvicorn.run(create_app(build_core()), host=HOST, port=port)


if __name__ == "__main__":
    main()
