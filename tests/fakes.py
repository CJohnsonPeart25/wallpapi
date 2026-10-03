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
from wallpapi.similarity import NOTHING_TO_CATCH_UP
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


class FakeSimilarityProvider:
    """Hand-defined similarities keyed by `(pool id, decided id)`: a **Wallpaper** against itself 1.0, as
    the protocol promises, and everything unnamed 0.0. `notice` and `vectors` are `None`, a provider at
    full strength with no positions, unless a test arranges them; an unnamed ID has no **Embedding**."""

    def __init__(
        self,
        similarities: dict[tuple[str, str], float] | None = None,
        *,
        notice: str | None = None,
        catch_up_waits: Sequence[float] = (),
        vectors: Mapping[str, NDArray[np.float32]] | None = None,
    ) -> None:
        self.similarity_by_pair = similarities or {}
        self.vector_by_id = None if vectors is None else dict(vectors)
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.notice_text = notice
        self.notice_pools: list[tuple[str, ...]] = []
        self.catch_up_calls: list[Path] = []
        self._caught_up = threading.Condition()
        self._catch_up_waits = list(catch_up_waits)

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        self.calls.append((tuple(p.id for p in pool), tuple(d.id for d in decided)))
        return np.array(
            [[self._between(p.id, d.id) for d in decided] for p in pool], dtype=np.float32
        ).reshape(len(pool), len(decided))

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Record the call and hand back the next arranged wait, then `NOTHING_TO_CATCH_UP` for ever."""
        del stop_event
        with self._caught_up:
            self.catch_up_calls.append(thumbnails)
            self._caught_up.notify_all()
        return self._catch_up_waits.pop(0) if self._catch_up_waits else NOTHING_TO_CATCH_UP

    def wait_for_catch_ups(self, count: int, timeout: float) -> bool:
        """Block until `catch_up` has been called `count` times, or `timeout` passes. Never a sleep."""
        with self._caught_up:
            return self._caught_up.wait_for(lambda: len(self.catch_up_calls) >= count, timeout)

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        self.notice_pools.append(tuple(w.id for w in pool))
        return self.notice_text

    def vectors(self, pool: Sequence[Wallpaper]) -> NDArray[np.float32] | None:
        if self.vector_by_id is None:
            return None
        width = next((v.size for v in self.vector_by_id.values()), 0)
        rows = np.zeros((len(pool), width), dtype=np.float32)
        for index, w in enumerate(pool):
            vector = self.vector_by_id.get(w.id)
            if vector is not None:
                rows[index] = vector
        return rows

    def _between(self, pool_id: str, decided_id: str) -> float:
        named = self.similarity_by_pair.get((pool_id, decided_id))
        if named is not None:
            return named
        return 1.0 if pool_id == decided_id else 0.0


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
