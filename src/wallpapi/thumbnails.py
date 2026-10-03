"""The **Thumbnail cache**: fetching and serving each tile's thumbnail, evicting by **Verdict** with a size
cap behind it, and the background downloader that fetches every **Pool** member's (ADR 0017).

The cache directory is handed in. Each file is named by its **Wallpaper** id, which is the whole mapping
from a file to a **Wallpaper**, and the **Similarity provider** reads the same directory by the same rule.
What is still to be shown and what has an **Explicit Verdict** standing are asked of `pool`, `batches` and
`decisions`; nothing here reads their tables.

Eviction runs at the tail of a submission and never on a **History** edit. A **Wallpaper** with an
**Explicit Verdict** is never evicted, by either pass, because **History** renders its thumbnail: a cache
over the cap on those alone stays over it and says so. Over-eager eviction costs one re-fetch, never data.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wallpapi import batches, decisions, pool, settings
from wallpapi.clock import Clock
from wallpapi.files import url_suffix, write_atomically
from wallpapi.wallhaven import REQUEST_TIMEOUT, RateLimited, ThumbnailUnavailable, Wallhaven

GAP_SECONDS = 0.25
"""The fixed gap between thumbnail fetches: a constant, never a setting."""

IDLE_RECHECK_SECONDS = 30.0
"""How long the downloader waits once no **Pool** member is missing a thumbnail."""

BACKOFF_SECONDS = 60.0
"""How long the downloader leaves the host alone after a 429 or a failed connection.

A `Retry-After` asking for longer is given longer; one asking for less does not shorten it, because the minute
is the downloader's own manners towards a host with no published limit.
"""

REFUSALS_BEFORE_GIVING_UP = 2
"""Refusals in a row before the downloader stops asking for a file until restart.

Without a limit a thumbnail gone for good is requested every pass and the page's coverage never reaches the
whole **Pool**. A 429 or a failed connection is not a refusal and never counts.
"""

THREAD_NAME = "wallpapi-thumbnails"

JOIN_TIMEOUT = REQUEST_TIMEOUT + 5.0
"""Seconds shutdown waits for the downloader: greater than the client's request timeout (invariant 12)."""

BYTES_IN_A_MEGABYTE = 1024 * 1024
"""Mebibytes, because matching what Explorer shows matters more than matching SI."""


@dataclass(frozen=True, slots=True)
class ThumbnailEviction:
    """What one eviction pass did. `over_cap` says "over the cap and not allowed to do anything about it"."""

    evicted: tuple[str, ...]
    remaining_bytes: int
    over_cap: bool


@dataclass(frozen=True, slots=True)
class _CachedThumbnail:
    """One file in the **Thumbnail cache**, stat-ed once."""

    wallpaper_id: str
    path: Path
    size: int
    modified_at: float


class Thumbnails:
    """The **Thumbnail cache** in one directory, and the downloader's own state.

    `get` and `evict` run on request threads; `wait` and `step` on the downloader's thread only, which is
    all that touches the downloader's state, so it needs no lock. Every call that reads the database is
    handed the caller's connection.
    """

    def __init__(self, directory: Path, wallhaven: Wallhaven, clock: Clock) -> None:
        self.directory = directory
        """Where the thumbnails are kept: separate from the **Library**, and read by the embedder too."""
        self._wallhaven = wallhaven
        self._clock = clock

        self._last_fetch: float | None = None
        self._not_before: float | None = None
        self._pass: deque[tuple[str, Path, str]] = deque()
        """What is left of the current pass round the **Pool**: `(wallpaper id, destination, source URL)`."""
        self._refusals: dict[str, int] = {}
        """Consecutive refusals per **Wallpaper**. A 429 or a failed connection is about the host and never
        counts.
        """
        self._given_up: set[str] = set()
        """Refused too often in a row: never asked for again in this process."""

    def get(self, connection: sqlite3.Connection, wallpaper_id: str) -> Path | None:
        """The cached thumbnail for a **Wallpaper**, fetched if missing; `None` for one this database has
        never seen.
        """
        row = connection.execute(
            "SELECT thumbnail_url FROM wallpapers WHERE id = ?", (wallpaper_id,)
        ).fetchone()
        if row is None:
            return None

        source_url = str(row["thumbnail_url"])
        destination = self._path_for(wallpaper_id, source_url)
        if destination.exists():
            return destination

        data = self._wallhaven.fetch_thumbnail(source_url)
        write_atomically(destination, data)
        return destination

    def evict(self, connection: sqlite3.Connection, cap_bytes: int) -> ThumbnailEviction:
        """Clear out the cache, by **Verdict** first and by size only as a backstop.

        First, every thumbnail with no **Explicit Verdict** standing, not in the **Pool** and not in the live
        **Batch**: nothing will ask for it again. Then, over `cap_bytes`, the oldest of those still to be
        shown. Neither pass evicts an **Explicit Verdict**, so a cache over the cap on **Favourites** alone
        says so in `over_cap`. A file that vanishes mid-pass is skipped: this runs after a submission and
        must not raise.
        """
        cached = _cached_thumbnails(self.directory)
        if not cached:
            return _NOTHING_EVICTED

        decided = decisions.explicitly_decided(connection)
        # The live **Batch** is named apart from the **Pool**: a **Filter** change prunes only the **Pool**.
        awaiting = {w.id for w in pool.members(connection)} | batches.showing(connection)
        evicted: list[str] = []
        capped: list[_CachedThumbnail] = []
        remaining = 0
        for thumbnail in cached:
            if thumbnail.wallpaper_id in decided:
                remaining += thumbnail.size
            elif thumbnail.wallpaper_id in awaiting:
                # Still to be shown, so kept unless the cap says otherwise.
                capped.append(thumbnail)
                remaining += thumbnail.size
            else:
                thumbnail.path.unlink(missing_ok=True)
                evicted.append(thumbnail.wallpaper_id)

        # Oldest modified first, the name breaking ties so the order never depends on the listing.
        for thumbnail in sorted(capped, key=lambda cached: (cached.modified_at, cached.path.name)):
            if remaining <= cap_bytes:
                break
            thumbnail.path.unlink(missing_ok=True)
            evicted.append(thumbnail.wallpaper_id)
            remaining -= thumbnail.size

        return ThumbnailEviction(
            evicted=tuple(evicted), remaining_bytes=remaining, over_cap=remaining > cap_bytes
        )

    def given_up(self) -> frozenset[str]:
        """What the downloader stopped asking for, left out of the page's coverage so the notice can clear."""
        return frozenset(self._given_up)

    def wait(self) -> float:
        """Seconds before the next `step`: the longer of the gap since the last fetch and any hold."""
        now = self._clock.monotonic()
        paced = gap_needed(self._last_fetch, now=now)
        held = 0.0 if self._not_before is None else self._not_before - now
        return max(paced, held, 0.0)

    def step(self, connection: sqlite3.Connection) -> None:
        """One step of the downloader: at most one fetch, never raising.

        A pass lists the **Pool** members missing a file, once, unless the cache is at its cap, and takes one
        per step; each is rechecked before its fetch. A refusal skips the file until the next pass, and a
        second in a row gives up on it until restart. A 429 or anything else backs off.
        """
        now = self._clock.monotonic()
        if not self._pass:
            if self._full(connection):
                self._not_before = now + IDLE_RECHECK_SECONDS
                return
            self._pass.extend(self._missing(connection))
            if not self._pass:
                self._not_before = now + IDLE_RECHECK_SECONDS
                return

        wallpaper_id, destination, source_url = self._pass.popleft()
        if not self._pass:
            # The end of a pass: what it failed to fetch is asked for once a pass, not four times a second.
            self._not_before = now + IDLE_RECHECK_SECONDS
        if destination.exists() or not pool.contains(connection, wallpaper_id):
            return

        self._last_fetch = now
        try:
            data = self._wallhaven.fetch_thumbnail(source_url)
        except ThumbnailUnavailable:
            refusals = self._refusals.get(wallpaper_id, 0) + 1
            self._refusals[wallpaper_id] = refusals
            if refusals >= REFUSALS_BEFORE_GIVING_UP:
                self._given_up.add(wallpaper_id)
            return
        except RateLimited as limited:
            self._not_before = now + max(limited.retry_after or 0.0, BACKOFF_SECONDS)
            return
        except Exception:
            self._not_before = now + BACKOFF_SECONDS
            return
        self._refusals.pop(wallpaper_id, None)
        try:
            write_atomically(destination, data)
        except OSError:
            # Still missing, so the next pass asks again. The thread must not die of one refused write.
            return

    def _missing(self, connection: sqlite3.Connection) -> list[tuple[str, Path, str]]:
        """Every **Pool** member with no cached thumbnail, in id order, less those given up on."""
        missing: list[tuple[str, Path, str]] = []
        for member in sorted(pool.members(connection), key=lambda w: w.id):
            if member.id in self._given_up:
                continue
            destination = self._path_for(member.id, member.thumbnail_url)
            if not destination.exists():
                missing.append((member.id, destination, member.thumbnail_url))
        return missing

    def _full(self, connection: sqlite3.Connection) -> bool:
        """Whether the cache is at or over `thumbnail_cache_max_mb`, read here because nothing hands the
        downloader's thread a cap.
        """
        held = sum(cached.size for cached in _cached_thumbnails(self.directory))
        return held >= settings.get(connection).thumbnail_cache_max_mb * BYTES_IN_A_MEGABYTE

    def _path_for(self, wallpaper_id: str, source_url: str) -> Path:
        return self.directory / f"{wallpaper_id}{url_suffix(source_url)}"


def download_loop(
    thumbnails: Thumbnails, connect: Callable[[], sqlite3.Connection], stop_event: threading.Event
) -> None:
    """Wait as `Thumbnails` says, take one step, repeat; `step` never raises. `connect` gives this thread its
    own connection.
    """
    while not stop_event.is_set():
        wait = thumbnails.wait()
        if wait > 0 and stop_event.wait(wait):
            return
        if stop_event.is_set():
            return
        thumbnails.step(connect())


def gap_needed(last_fetch: float | None, *, now: float, gap: float = GAP_SECONDS) -> float:
    """Seconds to wait before the next thumbnail fetch, or zero. A gap, not a window."""
    if last_fetch is None:
        return 0.0
    return max(0.0, last_fetch + gap - now)


def _cached_thumbnails(directory: Path) -> list[_CachedThumbnail]:
    """Every file in the cache, stat-ed once, keyed by the **Wallpaper** id its name carries; none for an
    absent directory.

    A file that vanishes before the stat is skipped: this runs after a submission and must not raise.
    """
    if not directory.is_dir():
        return []
    cached: list[_CachedThumbnail] = []
    for path in sorted(directory.iterdir()):
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            cached.append(
                _CachedThumbnail(
                    wallpaper_id=path.stem, path=path, size=stat.st_size, modified_at=stat.st_mtime
                )
            )
    return cached


_NOTHING_EVICTED = ThumbnailEviction(evicted=(), remaining_bytes=0, over_cap=False)
"""An empty or absent cache: nothing to delete and nothing taking up room."""
