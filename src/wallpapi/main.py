"""The entry point: `uv run python -m wallpapi`, and the one place the real dependencies are wired together.

Bound to `127.0.0.1` and single-worker: `--workers N` would mean N writers and N refill threads on one SQLite
file.
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
"""The **Similarity provider** wired in unless `WALLPAPI_SIMILARITY` says otherwise (ADR 0013).

`embedding` fetches its own model on first boot and falls back to the baseline until its cache fills.
`metadata` is the baseline; `tags` needs its cache filled by hand.
"""

MODEL_FILENAME = "clip-vit-b32-vision-quantized.onnx"
"""The image tower's name on disk, under `models/` in the wallpapi home. Named for what it is, so a new model
is a new file.
"""


def wallpapi_home() -> Path:
    """Where the **Decision log**, the **Thumbnail cache** and the model live."""
    configured = os.environ.get("WALLPAPI_HOME")
    return Path(configured) if configured else Path.home() / ".wallpapi"


def build_similarity(root: Path) -> SimilarityProvider:
    """Which **Similarity provider** to wire in, from `WALLPAPI_SIMILARITY` (ADR 0013).

    Each owns its own SQLite cache beside `wallpapi.db`. An unrecognised name raises rather than quietly
    running a different provider.
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
    """The real Core service. The seed is fresh per boot unless `WALLPAPI_SEED` pins it."""
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
    """The real app with its background threads.

    `refill=True` appears here and nowhere else, and nothing tests this function: lose that line and nothing
    fills.
    """
    return create_app(build_core(), refill=True)


def main() -> None:
    port = int(os.environ.get("WALLPAPI_PORT", DEFAULT_PORT))
    uvicorn.run(build_app(), host=HOST, port=port)


if __name__ == "__main__":
    main()
