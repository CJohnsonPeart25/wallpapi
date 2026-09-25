"""Fakes for the Core service's injected dependencies.

Four of the five dependencies are faked here. The fifth, the random source, is not: the spec calls for
"a seeded random source", so tests use the real implementation with a fixed seed rather than a fake.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Wallpaper
from wallpapi.wallhaven import SearchPage


def wallpaper(
    wallhaven_id: str,
    *,
    width: int = 3840,
    height: int = 2160,
    favourites: int = 100,
    category: str = "general",
) -> Wallpaper:
    """A Wallpaper shaped like one Wallhaven's search endpoint returns."""
    return Wallpaper(
        id=wallhaven_id,
        width=width,
        height=height,
        ratio="1.78",
        category=category,
        purity="sfw",
        favourites=favourites,
        colours=("#660000", "#000000"),
        thumbnail_url=f"https://th.wallhaven.cc/small/{wallhaven_id[:2]}/{wallhaven_id}.jpg",
        full_url=f"https://w.wallhaven.cc/full/{wallhaven_id[:2]}/wallhaven-{wallhaven_id}.jpg",
        page_url=f"https://wallhaven.cc/w/{wallhaven_id}",
    )


def catalogue_of(count: int, *, prefix: str = "wp") -> tuple[Wallpaper, ...]:
    """`count` distinct Wallpapers."""
    return tuple(wallpaper(f"{prefix}{n:04d}") for n in range(count))


THUMBNAIL_BYTES = b"\xff\xd8\xff\xe0 fake thumbnail"
"""What the fake serves for any thumbnail. A JPEG magic number, because Wallhaven's thumbs are `.jpg`."""


class FakeWallhavenClient:
    """An in-memory catalogue. Records the search parameters and the thumbnail URLs it was called with."""

    def __init__(self, catalogue: Sequence[Wallpaper] = (), *, seed: str | None = None) -> None:
        self.catalogue: list[Wallpaper] = list(catalogue)
        self.seed = seed
        self.searches: list[dict[str, object]] = []
        self.thumbnail_fetches: list[str] = []

    def search(self, *, sorting: str, purity: str, page: int = 1) -> SearchPage:
        self.searches.append({"sorting": sorting, "purity": purity, "page": page})
        return SearchPage(wallpapers=tuple(self.catalogue), seed=self.seed)

    def fetch_thumbnail(self, url: str) -> bytes:
        self.thumbnail_fetches.append(url)
        return THUMBNAIL_BYTES


class FakeLibraryWriter:
    """Records writes and deletions. Nothing in #2 should make it record anything."""

    def __init__(self) -> None:
        self.written: list[tuple[str, Path]] = []
        self.removed: list[Path] = []

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        del source_url
        self.written.append((wallpaper_id, destination))
        return destination

    def remove(self, path: Path) -> None:
        self.removed.append(path)


class FakeSimilarityProvider:
    """Hand-defined distances. Unused in #2 — Scoring and Zones arrive at #9."""

    def __init__(self, distances: dict[tuple[str, str], float] | None = None) -> None:
        self.distances = distances or {}
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def similarities(self, pool_ids: Sequence[str], decided_ids: Sequence[str]) -> NDArray[np.float32]:
        self.calls.append((tuple(pool_ids), tuple(decided_ids)))
        return np.array(
            [[self.distances.get((p, d), 0.0) for d in decided_ids] for p in pool_ids],
            dtype=np.float32,
        )


class FakeClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, at: dt.datetime) -> None:
        if at.tzinfo is not dt.UTC:
            raise ValueError("FakeClock must be given a UTC-aware datetime")
        self._now = at
        self._monotonic = 0.0

    def now(self) -> dt.datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)
        self._monotonic += seconds
