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
from fastapi import FastAPI

from wallpapi.clock import SystemClock
from wallpapi.core import CoreService
from wallpapi.library import DownloadingLibraryWriter
from wallpapi.rng import SeededRandom
from wallpapi.similarity import MetadataSimilarityProvider, SimilarityProvider
from wallpapi.similarity_embedding import (
    DownloadedModel,
    EmbeddingCache,
    EmbeddingSimilarityProvider,
)
from wallpapi.similarity_tags import TagCache, TagSimilarityProvider
from wallpapi.wallhaven import WallhavenClient
from wallpapi.web.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8000

DEFAULT_SIMILARITY = "embedding"
"""Which **Similarity provider** the app wires in unless `WALLPAPI_SIMILARITY` says otherwise.

Image embeddings, decided at #14 and recorded in ADR 0013. It fetches its own model on first boot and
falls back to the baseline for every **Wallpaper** it has not embedded yet, so there is nothing to install
and nothing to run first — a fresh wallpapi behaves exactly as the baseline did and gets better as its
cache fills, with a line on the page while that is happening.

The other two stay selectable because the maintainer has not yet seen all three against a real **Decision
log**, only against the spike's synthetic one. `metadata` is the baseline and costs nothing. `tags` needs
its cache filled by hand — about 500 **API calls** for a **Pool** at its default target size, out of
Wallhaven's 45 a minute — and until it is filled it behaves as the baseline.
"""

MODEL_FILENAME = "clip-vit-b32-vision-quantized.onnx"
"""The image tower's name on disk, under `models/` in the wallpapi home.

Beside the **Decision log** rather than inside the package: 85MiB of weights that are fetched once, are
not wallpapi's to ship, and can be deleted without reinstalling anything. Named for what it is, so that a
future change of model is a new file rather than a silent change of meaning behind one name.
"""


def wallpapi_home() -> Path:
    """Where the **Decision log**, the **Thumbnail cache** and the model live."""
    configured = os.environ.get("WALLPAPI_HOME")
    return Path(configured) if configured else Path.home() / ".wallpapi"


def build_similarity(root: Path) -> SimilarityProvider:
    """Which **Similarity provider** to wire in, from `WALLPAPI_SIMILARITY` (ADR 0013).

    Each provider owns its own SQLite cache beside `wallpapi.db`, which is why switching between them
    needs no migration and deleting one is deleting a file.

    Only the embedding provider is wired to something that *fills* its cache, and even then not here: it
    is handed a `ModelSource` and the background thread calls `catch_up`. Nothing on the path of a page
    load ever downloads a model or runs one.

    An unrecognised name raises rather than quietly falling back. The three behave differently and all
    three look the same from the page — a typo that silently ran the baseline would be a wallpapi scoring
    by colour with nothing anywhere saying so.
    """
    choice = os.environ.get("WALLPAPI_SIMILARITY", DEFAULT_SIMILARITY).strip().lower()
    if choice == "metadata":
        return MetadataSimilarityProvider()
    if choice == "tags":
        return TagSimilarityProvider(TagCache(root / "tags.db"))
    if choice == "embedding":
        return EmbeddingSimilarityProvider(
            EmbeddingCache(root / "embeddings.db"),
            model=DownloadedModel(root / "models" / MODEL_FILENAME),
        )
    raise ValueError(f"WALLPAPI_SIMILARITY must be metadata, tags or embedding — not {choice!r}")


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
        library=DownloadingLibraryWriter(),
        similarity=build_similarity(root),
        random_source=SeededRandom(int(pinned) if pinned else secrets.randbits(64)),
        clock=SystemClock(),
    )


def build_app() -> FastAPI:
    """The real app, with the background **Pool** refill running behind it.

    `refill=True` appears here and nowhere else. `create_app` leaves it off by default so that no test can
    start a thread that talks to Wallhaven, which makes this the one line that has to be right for the
    **Pool** to fill at all.
    """
    return create_app(build_core(), refill=True)


def main() -> None:
    port = int(os.environ.get("WALLPAPI_PORT", DEFAULT_PORT))
    uvicorn.run(build_app(), host=HOST, port=port)


if __name__ == "__main__":
    main()
