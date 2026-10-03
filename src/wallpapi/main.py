"""The entry point: `uv run python -m wallpapi`, wiring the real dependencies together. Bound to `127.0.0.1`
and single-worker (`--workers N` is N writers and N refill threads).
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
from wallpapi.similarity import DownloadedModel, EmbeddingCache, Embeddings, OnnxClipEmbedder
from wallpapi.wallhaven import WallhavenClient
from wallpapi.web.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8000

MODEL_FILENAME = "clip-vit-b32-vision-quantized.onnx"
"""The image tower's name on disk, under `models/` in the wallpapi home."""


def wallpapi_home() -> Path:
    """Where the **Decision log**, the **Thumbnail cache** and the model live."""
    configured = os.environ.get("WALLPAPI_HOME")
    return Path(configured) if configured else Path.home() / ".wallpapi"


def build_similarity(root: Path) -> Embeddings:
    """The **Similarity provider** (ADR 0013): nothing opened, imported or fetched until its thread asks."""
    model = root / "models" / MODEL_FILENAME
    return Embeddings(EmbeddingCache(root / "embeddings.db"), OnnxClipEmbedder(model), DownloadedModel(model))


def build_core(home: Path | None = None) -> CoreService:
    """The real Core service. The seed is fresh per boot unless `WALLPAPI_SEED` pins it; the Refill draws its
    own stream from the one after it.
    """
    root = wallpapi_home() if home is None else home
    root.mkdir(parents=True, exist_ok=True)
    pinned = os.environ.get("WALLPAPI_SEED")
    seed = int(pinned) if pinned else secrets.randbits(64)
    return CoreService(
        db_path=root / "wallpapi.db",
        thumbnail_dir=root / "thumbnails",
        wallhaven=WallhavenClient(),
        library=DownloadingLibraryWriter(),
        similarity=build_similarity(root),
        random_source=SeededRandom(seed),
        # Its own stream, so a refill step never changes which **Batch** a seed draws.
        refill_random_source=SeededRandom(seed + 1),
        clock=SystemClock(),
    )


def build_app() -> FastAPI:
    """The real app with its background threads. `refill=True` appears here and nowhere else, and nothing
    tests it: lose that line and nothing fills.
    """
    return create_app(build_core(), refill=True)


def main() -> None:
    port = int(os.environ.get("WALLPAPI_PORT", DEFAULT_PORT))
    uvicorn.run(build_app(), host=HOST, port=port)


if __name__ == "__main__":
    main()
