"""Fakes for the Core service's injected dependencies.

Four of the five dependencies are faked here. The fifth, the random source, is not: the spec calls for
"a seeded random source", so tests use the real implementation with a fixed seed rather than a fake.
"""

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
    """What the fake raises to stand in for a transport failure.

    The protocol names one error and one only — `RateLimited`, because a 429 is the one failure the caller
    treats differently. Everything else is "the call did not happen", and this exists so a test can arrange
    that without reaching for `httpx2` on the far side of the seam.
    """


class FakeWallhavenClient:
    """An in-memory catalogue, served a page at a time.

    Records the search parameters and the thumbnail URLs it was called with. `page_size` defaults to
    Wallhaven's listing size of 24; tests that care about a walk turn it down so a page boundary is a
    couple of **Wallpapers** away rather than two dozen.

    `seed` is what this fake returns as `meta.seed`. It ignores the seed it is *given* when choosing what to
    serve, which is the one place it is deliberately less than faithful: Wallhaven reshuffles, and a fake
    that did would make every refill test depend on a shuffle nobody chose. What the seed is actually for —
    being carried across the pages of one walk and not across walks — is asserted from `searches` instead.

    `rate_limited_calls` makes the first N searches answer 429, as the real client's `RateLimited`.
    `fail_from_call` makes every call from the Nth onwards a transport failure. Together they cover both
    halves of the refill's error handling.

    `like_results` is the second catalogue: what a `q=like:<wallhaven id>` search answers, keyed by that
    ID (#13). A **Favourite** with no entry answers an empty page, which is also how a real like: search
    ends — Wallhaven has only so many lookalikes to offer for any one **Wallpaper**.
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
        self.searched = threading.Event()
        """Set by every search. The one thread test waits on this rather than guessing how long the
        refill thread needs, so it is deterministic without sleeping."""

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
        """Which catalogue answers this search: the lookalikes of one **Wallpaper**, or everything.

        Only `like:` is understood, because it is the only expression wallpapi sends. Anything else would
        be a search this fake has been asked to answer without having been told how to.
        """
        if query is None:
            return self.catalogue
        if not query.startswith(LIKE_QUERY_PREFIX):
            raise ValueError(f"the fake was not told how to answer {query!r}")
        return self.like_results.get(query.removeprefix(LIKE_QUERY_PREFIX), [])

    def fetch_thumbnail(self, url: str) -> bytes:
        self.thumbnail_fetches.append(url)
        return self.thumbnail_bytes


@dataclass(frozen=True, slots=True)
class LibraryWrite:
    """One call the **Library** writer was asked to make.

    Carries the source URL as well as the destination, so a test can pin that a **Favourite** downloads the
    full-resolution image rather than the thumbnail.
    """

    wallpaper_id: str
    source_url: str
    destination: Path


class LibraryUnwritable(RuntimeError):
    """What the fake raises to stand in for a failed download or a failed disk write.

    The **Library** protocol declares no error type — the real writer can fail with anything `httpx2` or the
    filesystem raises — so this exists to let a test make a write fail without reaching for either.
    """


class FakeLibraryWriter:
    """Records writes and deletions, and can be told to fail for particular **Wallpapers**.

    `fail_for` is a mutable set so that one test can make a write fail, assert the **Decision log** survived
    it, then empty the set and prove the next reconciliation retries rather than having given up.
    """

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
    """Hand-defined similarities, keyed by `(pool wallpaper id, decided wallpaper id)`.

    Takes whole **Wallpapers**, as the protocol does — the real provider reads their colours and category —
    but keys on the IDs, so arranging a similarity in a test is still a pair of strings and a number.

    A **Wallpaper** against itself is 1.0 unless a test says otherwise, because that is what the protocol
    promises and because "a **Favourite** still in the **Pool** scores its own value at distance 0" should
    not have to be arranged. Everything not named is 0.0: nothing in common.
    """

    def __init__(
        self,
        similarities: dict[tuple[str, str], float] | None = None,
        *,
        notice: str | None = None,
        catch_up_waits: Sequence[float] = (),
    ) -> None:
        self.similarity_by_pair = similarities or {}
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.notice_text = notice
        """What `notice()` answers. `None` — a provider working at full strength — unless a test says
        otherwise, because every test that is not about the notice wants no extra line on the page."""
        self.catch_up_calls: list[Path] = []
        self.catch_up_started = threading.Event()
        """Set by the first `catch_up`. The thread test waits on this rather than guessing how long the
        thread needs, the same way `FakeWallhavenClient.searched` does."""
        self._catch_up_waits = list(catch_up_waits)

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        self.calls.append((tuple(p.id for p in pool), tuple(d.id for d in decided)))
        return np.array(
            [[self._between(p.id, d.id) for d in decided] for p in pool], dtype=np.float32
        ).reshape(len(pool), len(decided))

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Record the call and hand back the next arranged wait, then `NOTHING_TO_CATCH_UP` for ever.

        A list of waits rather than one number, so a test can arrange "more to do, then done" and watch
        the loop come straight back before it settles.
        """
        del stop_event
        self.catch_up_calls.append(thumbnails)
        self.catch_up_started.set()
        return self._catch_up_waits.pop(0) if self._catch_up_waits else NOTHING_TO_CATCH_UP

    def notice(self) -> str | None:
        return self.notice_text

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
