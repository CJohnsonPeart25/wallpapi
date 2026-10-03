"""Fakes for four of the Core service's five injected dependencies. The fifth, the random source, is the
real `SeededRandom` with a fixed seed, as the spec asks for a seeded source."""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from wallpapi.core import LIKE_QUERY_PREFIX
from wallpapi.model import Wallpaper
from wallpapi.wallhaven import RateLimited, SearchPage


def wallpaper(
    wallhaven_id: str,
    *,
    width: int = 3840,
    height: int = 2160,
    favourites: int = 100,
    category: str = "general",
    colours: tuple[str, ...] = ("#660000", "#000000"),
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
        colours=colours,
        thumbnail_url=f"https://th.wallhaven.cc/small/{wallhaven_id[:2]}/{wallhaven_id}.jpg",
        full_url=f"https://w.wallhaven.cc/full/{wallhaven_id[:2]}/wallhaven-{wallhaven_id}.jpg",
        page_url=f"https://wallhaven.cc/w/{wallhaven_id}",
    )


def catalogue_of(count: int, *, prefix: str = "wp") -> tuple[Wallpaper, ...]:
    """`count` distinct Wallpapers."""
    return tuple(wallpaper(f"{prefix}{n:04d}") for n in range(count))


THUMBNAIL_BYTES = b"\xff\xd8\xff\xe0 fake thumbnail"
"""What the fake serves for any thumbnail. A JPEG magic number, because Wallhaven's thumbs are `.jpg`."""


class WallhavenUnreachable(RuntimeError):
    """A transport failure. The protocol names only `RateLimited`; anything else did not happen."""


class FakeWallhavenClient:
    """An in-memory catalogue served a page at a time, recording every search and thumbnail fetch.

    `seed` is returned as `meta.seed`, and the seed it is given is ignored: Wallhaven reshuffles, and a fake
    that did would make every refill test depend on a shuffle nobody chose. `rate_limited_calls` answers
    the first N searches 429; `fail_from_call` fails every call from the Nth. `like_results` answers
    `q=like:<id>`, keyed by that ID, an unnamed one with an empty page. `failing_thumbnails` maps a URL to
    what fetching it raises and is mutable, so a test can watch a retry. `hold_thumbnails` keeps every
    fetch in flight until a test sets it.
    """

    def __init__(
        self,
        catalogue: Sequence[Wallpaper] = (),
        *,
        seed: str | None = None,
        page_size: int = 24,
        thumbnail_bytes: bytes = THUMBNAIL_BYTES,
        fail_from_call: int | None = None,
        rate_limited_calls: int = 0,
        retry_after: float | None = None,
        like_results: Mapping[str, Sequence[Wallpaper]] | None = None,
    ) -> None:
        self.catalogue: list[Wallpaper] = list(catalogue)
        self.like_results: dict[str, list[Wallpaper]] = {
            key: list(value) for key, value in (like_results or {}).items()
        }
        self.seed = seed
        self.page_size = page_size
        self.thumbnail_bytes = thumbnail_bytes
        self.fail_from_call = fail_from_call
        self.rate_limited_calls = rate_limited_calls
        self.retry_after = retry_after
        self.searches: list[dict[str, object]] = []
        self.thumbnail_fetches: list[str] = []
        self.failing_thumbnails: dict[str, Exception] = {}
        self.thumbnail_threads: list[str] = []
        """The name of the thread each thumbnail fetch was made on."""
        self.thumbnail_fetched = threading.Event()
        self.hold_thumbnails: threading.Event | None = None
        self.searched = threading.Event()
        """Set by every search, so a thread test waits on an event rather than a guess."""

    def search(
        self,
        *,
        sorting: str,
        purity: str,
        categories: str | None = None,
        query: str | None = None,
        page: int = 1,
        seed: str | None = None,
        atleast: str | None = None,
        ratios: str | None = None,
    ) -> SearchPage:
        self.searches.append(
            {
                "sorting": sorting,
                "purity": purity,
                "categories": categories,
                "query": query,
                "page": page,
                "seed": seed,
                "atleast": atleast,
                "ratios": ratios,
            }
        )
        self.searched.set()
        if len(self.searches) <= self.rate_limited_calls:
            raise RateLimited(self.retry_after)
        if self.fail_from_call is not None and len(self.searches) >= self.fail_from_call:
            raise WallhavenUnreachable(f"call {len(self.searches)} was set up to fail")
        results = self._results_for(query)
        start = (page - 1) * self.page_size
        return SearchPage(wallpapers=tuple(results[start : start + self.page_size]), seed=self.seed)

    def _results_for(self, query: str | None) -> list[Wallpaper]:
        """Only `like:` is understood, because it is the only expression wallpapi sends."""
        if query is None:
            return self.catalogue
        if not query.startswith(LIKE_QUERY_PREFIX):
            raise ValueError(f"the fake was not told how to answer {query!r}")
        return self.like_results.get(query.removeprefix(LIKE_QUERY_PREFIX), [])

    def fetch_thumbnail(self, url: str) -> bytes:
        self.thumbnail_fetches.append(url)
        self.thumbnail_threads.append(threading.current_thread().name)
        self.thumbnail_fetched.set()
        if self.hold_thumbnails is not None:
            self.hold_thumbnails.wait()
        failure = self.failing_thumbnails.get(url)
        if failure is not None:
            raise failure
        return self.thumbnail_bytes


@dataclass(frozen=True, slots=True)
class LibraryWrite:
    """One call the **Library** writer was asked to make, source URL included."""

    wallpaper_id: str
    source_url: str
    destination: Path


class LibraryUnwritable(RuntimeError):
    """A failed download or disk write. The protocol declares no error type of its own."""


class FakeLibraryWriter:
    """Records writes and deletions. `fail_for` is mutable, so a test can make a write fail and then
    watch the next reconciliation retry it."""

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.written: list[LibraryWrite] = []
        self.removed: list[Path] = []
        self.fail_for: set[str] = set() if fail_for is None else fail_for

    def write(self, wallpaper_id: str, source_url: str, destination: Path) -> Path:
        if wallpaper_id in self.fail_for:
            raise LibraryUnwritable(f"{wallpaper_id} was set up to fail")
        self.written.append(
            LibraryWrite(wallpaper_id=wallpaper_id, source_url=source_url, destination=destination)
        )
        return destination

    def remove(self, path: Path) -> None:
        self.removed.append(path)


class FakeSimilarities:
    """The injected similarity matrix: hand-defined similarities keyed by `(pool id, decided id)`, a
    **Wallpaper** against itself 1.0 and everything unnamed 0.0. `Embeddings` falls back to it for every pair
    without two **Embeddings**, which in the harness is every pair unless a test stores vectors."""

    def __init__(self, similarities: dict[tuple[str, str], float] | None = None) -> None:
        self.similarity_by_pair = similarities or {}
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def __call__(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        self.calls.append((tuple(p.id for p in pool), tuple(d.id for d in decided)))
        return np.array(
            [[self._between(p.id, d.id) for d in decided] for p in pool], dtype=np.float32
        ).reshape(len(pool), len(decided))

    def _between(self, pool_id: str, decided_id: str) -> float:
        named = self.similarity_by_pair.get((pool_id, decided_id))
        if named is not None:
            return named
        return 1.0 if pool_id == decided_id else 0.0


class MemoryStore:
    """An `EmbeddingStore` in a dict, holding vectors as given."""

    def __init__(self, vectors: Mapping[str, NDArray[np.float32]] | None = None) -> None:
        self.vector_by_id = dict(vectors or {})

    def vectors_for(self, wallpaper_ids: Sequence[str]) -> Mapping[str, NDArray[np.float32]]:
        return {w: self.vector_by_id[w] for w in wallpaper_ids if w in self.vector_by_id}

    def store(self, wallpaper_id: str, vector: NDArray[np.float32]) -> None:
        self.vector_by_id[wallpaper_id] = vector

    def embedded_ids(self) -> set[str]:
        return set(self.vector_by_id)


class ModelOnDisk:
    """A `ModelSource` that is simply already there. Counts how often it was asked, and says when it was."""

    def __init__(self, path: Path = Path("model.onnx")) -> None:
        self.path = path
        self.calls = 0
        self.asked = threading.Event()

    def ensure(self, stop_event: threading.Event) -> Path:
        del stop_event
        self.calls += 1
        self.asked.set()
        return self.path


class ModelThatFails:
    """A `ModelSource` that cannot be had: no network, or a checksum that did not match."""

    def __init__(self, message: str = "connection refused") -> None:
        self.message = message

    def ensure(self, stop_event: threading.Event) -> Path:
        del stop_event
        raise RuntimeError(self.message)


STUB_DIRECTION = np.array([1.0, 0.0], dtype=np.float32)


class StubEmbed:
    """An `Embed` that gives every file `STUB_DIRECTION` and records what it was given. Called with no files
    it opens nothing, or raises if `will_not_open`, as a model file that is not a graph would."""

    def __init__(self, *, unreadable: Sequence[str] = (), will_not_open: bool = False) -> None:
        self.seen: list[str] = []
        self.batches: list[int] = []
        self.unreadable = set(unreadable)
        self.will_not_open = will_not_open
        self._embedded = threading.Condition()

    def __call__(self, images: Sequence[Path]) -> NDArray[np.float32]:
        if not images:
            if self.will_not_open:
                raise RuntimeError("INVALID_PROTOBUF : Load model failed")
            return np.zeros((0, len(STUB_DIRECTION)), dtype=np.float32)
        if any(path.stem in self.unreadable for path in images):
            raise OSError("cannot identify image file")
        with self._embedded:
            self.batches.append(len(images))
            self.seen.extend(path.stem for path in images)
            self._embedded.notify_all()
        return np.stack([STUB_DIRECTION for _ in images])

    def wait_for(self, count: int, timeout: float) -> bool:
        """Block until `count` files have been embedded, or `timeout` passes. Never a sleep."""
        with self._embedded:
            return self._embedded.wait_for(lambda: len(self.seen) >= count, timeout)


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
