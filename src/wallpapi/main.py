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
from wallpapi.similarity_embedding import EmbeddingCache, EmbeddingSimilarityProvider
from wallpapi.similarity_tags import TagCache, TagSimilarityProvider
from wallpapi.wallhaven import WallhavenClient
from wallpapi.web.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8000

DEFAULT_SIMILARITY = "metadata"
"""Which **Similarity provider** the app wires in unless `WALLPAPI_SIMILARITY` says otherwise.

The baseline, and it stays the baseline until the maintainer settles #14. The other two are that spike's
candidates: `tags` wants a filled tag cache, which costs an **API call** per **Wallpaper**, and
`embedding` wants the `similarity-embedding` extra, the model file and a filled embedding cache. Both fall
back to the baseline for any **Wallpaper** their cache has not reached, so either is safe to switch on
against a half-filled cache — it simply behaves more like the baseline the emptier the cache is.
"""


def wallpapi_home() -> Path:
    """Where the **Decision log** and the **Thumbnail cache** live."""
    configured = os.environ.get("WALLPAPI_HOME")
    return Path(configured) if configured else Path.home() / ".wallpapi"


def model_path(home: Path | None = None) -> Path:
    """Where the CLIP image tower the embedding provider needs is kept (#14).

    Beside the **Decision log** rather than in the package: it is 85MiB of weights that are downloaded
    once, are not wallpapi's to ship, and can be deleted without reinstalling anything.
    """
    root = wallpapi_home() if home is None else home
    return root / "models" / "clip-vit-b32-vision-quantized.onnx"


def build_similarity(root: Path) -> SimilarityProvider:
    """Which **Similarity provider** to wire in, from `WALLPAPI_SIMILARITY` (#14).

    Each candidate owns its own SQLite cache beside `wallpapi.db`, which is why switching between them
    needs no migration and deleting one is deleting a file. Neither candidate is wired to anything that
    *fills* its cache: the app reads what is there and falls back to the baseline for the rest. Filling is
    a step somebody runs on purpose, because one costs **API calls** out of the refill's 45 a minute and
    the other costs a model run per **Wallpaper**, and neither belongs on the path of a page load.

    An unrecognised name raises rather than falling back to the baseline. A typo that silently ran the
    provider being compared *against* would make #14's whole comparison a lie, and this is the one line
    that decides which one the numbers came from.
    """
    choice = os.environ.get("WALLPAPI_SIMILARITY", DEFAULT_SIMILARITY).strip().lower()
    if choice == "metadata":
        return MetadataSimilarityProvider()
    if choice == "tags":
        return TagSimilarityProvider(TagCache(root / "tags.db"))
    if choice == "embedding":
        return EmbeddingSimilarityProvider(EmbeddingCache(root / "embeddings.db"))
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
