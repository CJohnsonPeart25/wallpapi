"""The Core service: the interface between the UI and everything else, with SQLite as an in-process detail."""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections import deque
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path

from wallpapi import batches, decisions, similarity, storage
from wallpapi import pool as pool_module
from wallpapi import settings as settings_module
from wallpapi.allocation import ScoredWallpaper as ScoredWallpaper
from wallpapi.background import BackgroundLoop
from wallpapi.batches import Batch as Batch
from wallpapi.batches import BatchUnavailable as BatchUnavailable
from wallpapi.batches import SubmissionRefused as SubmissionRefused
from wallpapi.batches import Submitted as Submitted
from wallpapi.clock import Clock
from wallpapi.decisions import ResolvedVerdict
from wallpapi.files import url_suffix, write_atomically
from wallpapi.library import FavouriteDownload, Library, LibraryReconciliation, LibraryWriter
from wallpapi.model import DecisionEntry, Mix, Verdict, Wallpaper
from wallpapi.pool import wallpaper_from_row
from wallpapi.rng import SeededRandom

# Re-exported (`X as X`) for the tests that still import them from here, until #64 and #68 move them.
from wallpapi.settings import EXPLORE_MIX as EXPLORE_MIX
from wallpapi.settings import MAX_BATCH_SIZE as MAX_BATCH_SIZE
from wallpapi.settings import REFINE_MIX as REFINE_MIX
from wallpapi.settings import SUPERSEDED_POOL_TARGET_SIZE as SUPERSEDED_POOL_TARGET_SIZE
from wallpapi.settings import SUPERSEDED_SIMILARITY_RADIUS as SUPERSEDED_SIMILARITY_RADIUS
from wallpapi.settings import MixListing, Settings, SettingsRefused
from wallpapi.similarity import Embeddings
from wallpapi.wallhaven import REQUEST_TIMEOUT, RateLimited, ThumbnailUnavailable, Wallhaven

THUMBNAIL_GAP_SECONDS = 0.25
"""The fixed gap between thumbnail fetches: a constant, never a setting."""

THUMBNAIL_IDLE_RECHECK_SECONDS = 30.0
"""How long the thumbnail downloader waits once no **Pool** member is missing a thumbnail."""

THUMBNAIL_BACKOFF_SECONDS = 60.0
"""How long the thumbnail downloader leaves the host alone after a 429 or a failed connection.

A `Retry-After` asking for longer is given longer; one asking for less does not shorten it, because the minute
is the downloader's own manners towards a host with no published limit.
"""

THUMBNAIL_REFUSALS_BEFORE_GIVING_UP = 2
"""Refusals in a row before the thumbnail downloader stops asking for a file until restart.

Without a limit a thumbnail gone for good is requested every pass and the page's coverage never reaches the
whole **Pool**. A 429 or a failed connection is not a refusal and never counts.
"""

THUMBNAIL_THREAD_NAME = "wallpapi-thumbnails"

THUMBNAIL_JOIN_TIMEOUT = REQUEST_TIMEOUT + 5.0
"""Seconds shutdown waits for the downloader: greater than the client's request timeout (invariant 12)."""

BYTES_IN_A_MEGABYTE = 1024 * 1024
"""Mebibytes, because matching what Explorer shows matters more than matching SI."""


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One **Wallpaper**'s line in **History**, not one entry: its resolved **Verdict** and its latest
    timestamp.
    """

    wallpaper: Wallpaper
    resolved: ResolvedVerdict
    latest_at: dt.datetime


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of **History**. `total` counts every matching row, so the page can say paging is happening."""

    rows: tuple[HistoryRow, ...]
    page: int
    pages: int
    total: int
    verdict: Verdict | None

    @property
    def previous_page(self) -> int | None:
        return self.page - 1 if self.page > 1 else None

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.page < self.pages else None


@dataclass(frozen=True)
class HistoryRefused:
    """A **History** edit did not happen, and this is why. Only a hand-made post or a second tab can cause
    one.
    """

    class Reason(StrEnum):
        UNKNOWN_WALLPAPER = "unknown_wallpaper"
        """No such **Wallpaper** in this database."""

    reason: Reason


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


class CoreService:
    def __init__(
        self,
        *,
        db_path: Path,
        wallhaven: Wallhaven,
        library: LibraryWriter,
        similarity: Embeddings,
        random_source: SeededRandom,
        refill_random_source: SeededRandom,
        clock: Clock,
    ) -> None:
        self._db_path = db_path
        self._wallhaven = wallhaven
        self._similarity = similarity
        self._clock = clock
        self._connections = storage.ThreadConnections(db_path)

        # The thumbnail downloader's own state. Touched only by its thread, so it needs no lock.
        self._last_thumbnail_fetch: float | None = None
        self._thumbnails_not_before: float | None = None
        self._thumbnail_pass: deque[tuple[str, Path, str]] = deque()
        """What is left of the current pass round the **Pool**: `(wallpaper id, destination, source URL)`."""
        self._thumbnail_refusals: dict[str, int] = {}
        """Consecutive refusals per **Wallpaper**. A 429 or a failed connection is about the host and never
        counts.
        """
        self._thumbnails_given_up: set[str] = set()
        """Refused too often in a row: never asked for again in this process, and left out of the page's
        coverage.
        """

        storage.migrate(self._connect())
        self.refill = pool_module.Refill(self._connect, wallhaven, clock, refill_random_source)
        """The background search that keeps the **Pool** stocked: the thread drives it, the page reads it."""
        self.batches = batches.Batches(similarity, self.refill.status, clock, random_source)
        """Minting and submitting **Batches**, with the draw's own random source."""
        self.library = Library(library, clock)
        """Reconciliation and the **Favourite** download, run after the **Decision log** commits."""

    # -- storage ---------------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return self._connections.get()

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        with storage.write(self._connect()) as connection:
            yield connection

    # -- settings --------------------------------------------------------------------------------------
    # Delegations to the settings module, kept until the callers move to it (#64, #68).

    def get_settings(self) -> Settings:
        """Everything configurable; see `settings.get`."""
        return settings_module.get(self._connect())

    def update_settings(self, **fields: object) -> Settings | SettingsRefused:
        """Validate and store the settings named, then prune the **Pool** to the **Filters**, in one write
        transaction; see `settings.update`.
        """
        with self._write() as write:
            updated = settings_module.update(write, **fields)
            if isinstance(updated, Settings):
                pool_module.prune(write, updated)
            return updated

    # -- mixes -----------------------------------------------------------------------------------------

    def list_mixes(self) -> tuple[Mix, ...]:
        """Every stored **Mix**, by name; see `settings.list_mixes`."""
        return tuple(listed.mix for listed in self.mix_listings())

    def mix_listings(self) -> tuple[MixListing, ...]:
        """Every stored **Mix**, by name, each saying whether it may be deleted."""
        return settings_module.list_mixes(self._connect())

    def active_mix(self) -> Mix:
        """The **Mix** the next **Batch** is built from; see `settings.active_mix`."""
        return settings_module.active_mix(self._connect())

    def save_mix(
        self, name: str, *, unknown: int | str, banger: int | str, dud: int | str
    ) -> Mix | SettingsRefused:
        """Store a **Mix** under that name; see `settings.save_mix`."""
        with self._write() as write:
            return settings_module.save_mix(write, name, unknown=unknown, banger=banger, dud=dud)

    def delete_mix(self, name: str) -> SettingsRefused | None:
        """Remove a **Mix**, or say why it stays; see `settings.delete_mix`."""
        with self._write() as write:
            return settings_module.delete_mix(write, name)

    # -- batches ---------------------------------------------------------------------------------------

    def get_next_batch(self) -> Batch | BatchUnavailable:
        """The live **Batch**, or a new one: `Batches.next`."""
        return self.batches.next(self._connect())

    def classify_pool(self) -> tuple[ScoredWallpaper, ...]:
        """The classified **Pool**: `Batches.classify`."""
        return self.batches.classify(self._connect())

    # -- the Similarity provider's own upkeep ------------------------------------------------------------

    def similarity_notice(self) -> str | None:
        """One line for the page when the **Similarity provider** is not at full strength.

        Handed the **Pool** less what the thumbnail downloader gave up on, so the line clears once everything
        that can be embedded has been.
        """
        given_up = self._thumbnails_given_up
        return self._similarity.notice(
            [w for w in pool_module.members(self._connect()) if w.id not in given_up]
        )

    def similarity_step(self, stop_event: threading.Event) -> float:
        """One step of the **Similarity provider**'s upkeep, on its background thread only; seconds to the
        next.
        """
        return self._similarity.catch_up(self.thumbnail_dir, stop_event)

    def background_loops(self) -> tuple[BackgroundLoop, ...]:
        """The refill, the **Similarity provider**'s upkeep and the thumbnail downloader, in the order the
        lifespan starts them; it stops them in reverse.
        """
        return (
            BackgroundLoop(
                partial(pool_module.refill_loop, self.refill),
                name=pool_module.THREAD_NAME,
                join_timeout=pool_module.JOIN_TIMEOUT,
            ),
            BackgroundLoop(
                partial(similarity.upkeep_loop, self._similarity, self.thumbnail_dir),
                name=similarity.THREAD_NAME,
                join_timeout=similarity.JOIN_TIMEOUT,
            ),
            BackgroundLoop(
                partial(thumbnail_loop, self),
                name=THUMBNAIL_THREAD_NAME,
                join_timeout=THUMBNAIL_JOIN_TIMEOUT,
            ),
        )

    def set_draft_verdict(
        self, batch_id: str, wallpaper_id: str, verdict: Verdict | None
    ) -> Batch | SubmissionRefused:
        """Mark one tile, or clear it: `batches.set_draft` in its own transaction."""
        with self._write() as write:
            return batches.set_draft(write, batch_id, wallpaper_id, verdict)

    def set_all_draft_verdicts(self, batch_id: str, verdict: Verdict | None) -> Batch | SubmissionRefused:
        """Select-all or select-none: `batches.set_all_drafts` in its own transaction."""
        with self._write() as write:
            return batches.set_all_drafts(write, batch_id, verdict)

    def submit_batch(self, batch_id: str) -> Submitted | SubmissionRefused:
        """Submit the **Batch** in one transaction, then reconcile the **Library** and evict thumbnails."""
        with self._write() as write:
            submitted = self.batches.submit(write, batch_id)
        if isinstance(submitted, SubmissionRefused):
            return submitted

        # Outside the transaction, deliberately: a download is a network call, and nothing the **Library**
        # does may roll the **Decision log** back. Idempotent, so a failure is picked up next time.
        self.reconcile_library()
        # Before the next **Batch** is drawn, so nothing about to be drawn is being counted as evictable.
        self.evict_thumbnails()
        return submitted

    # -- library ---------------------------------------------------------------------------------------

    def reconcile_library(self) -> LibraryReconciliation:
        """Make the **Library** folder agree with the **Decision log**: `Library.reconcile`."""
        return self.library.reconcile(self._connect(), self.get_settings().library_path)

    def download_favourites(self) -> FavouriteDownload:
        """Write a **Library** file for every **Favourite** without one: `Library.download_favourites`."""
        return self.library.download_favourites(self._connect(), self.get_settings().library_path)

    # -- thumbnails ------------------------------------------------------------------------------------

    @property
    def thumbnail_dir(self) -> Path:
        """The **Thumbnail cache** directory, separate from the **Library**."""
        return self._db_path.parent / "thumbnails"

    def get_thumbnail(self, wallpaper_id: str) -> Path | None:
        """The cached thumbnail for a **Wallpaper**, fetched if missing; `None` for one this database has
        never seen.
        """
        row = (
            self._connect()
            .execute("SELECT thumbnail_url FROM wallpapers WHERE id = ?", (wallpaper_id,))
            .fetchone()
        )
        if row is None:
            return None

        source_url = str(row["thumbnail_url"])
        destination = self.thumbnail_dir / f"{wallpaper_id}{url_suffix(source_url)}"
        if destination.exists():
            return destination

        data = self._wallhaven.fetch_thumbnail(source_url)
        write_atomically(destination, data)
        return destination

    def thumbnail_wait(self) -> float:
        """Seconds before the next `thumbnail_step`: the longer of the gap since the last fetch and any
        hold.
        """
        now = self._clock.monotonic()
        paced = gap_needed(self._last_thumbnail_fetch, now=now)
        held = 0.0 if self._thumbnails_not_before is None else self._thumbnails_not_before - now
        return max(paced, held, 0.0)

    def thumbnail_step(self) -> None:
        """One step of the thumbnail downloader: at most one fetch, never raising (ADR 0017).

        A pass lists the **Pool** members missing a file, once, unless the cache is at its cap, and takes one
        per step; each is rechecked before its fetch. A refusal skips the file until the next pass, and a
        second in a row gives up on it until restart. A 429 or anything else backs off.
        """
        now = self._clock.monotonic()
        if not self._thumbnail_pass:
            if self._thumbnail_cache_full():
                self._thumbnails_not_before = now + THUMBNAIL_IDLE_RECHECK_SECONDS
                return
            self._thumbnail_pass.extend(self._pool_missing_thumbnails())
            if not self._thumbnail_pass:
                self._thumbnails_not_before = now + THUMBNAIL_IDLE_RECHECK_SECONDS
                return

        wallpaper_id, destination, source_url = self._thumbnail_pass.popleft()
        if not self._thumbnail_pass:
            # The end of a pass: what it failed to fetch is asked for once a pass, not four times a second.
            self._thumbnails_not_before = now + THUMBNAIL_IDLE_RECHECK_SECONDS
        if destination.exists() or not self._in_pool(wallpaper_id):
            return

        self._last_thumbnail_fetch = now
        try:
            data = self._wallhaven.fetch_thumbnail(source_url)
        except ThumbnailUnavailable:
            refusals = self._thumbnail_refusals.get(wallpaper_id, 0) + 1
            self._thumbnail_refusals[wallpaper_id] = refusals
            if refusals >= THUMBNAIL_REFUSALS_BEFORE_GIVING_UP:
                self._thumbnails_given_up.add(wallpaper_id)
            return
        except RateLimited as limited:
            self._thumbnails_not_before = now + max(limited.retry_after or 0.0, THUMBNAIL_BACKOFF_SECONDS)
            return
        except Exception:
            self._thumbnails_not_before = now + THUMBNAIL_BACKOFF_SECONDS
            return
        self._thumbnail_refusals.pop(wallpaper_id, None)
        try:
            write_atomically(destination, data)
        except OSError:
            # Still missing, so the next pass asks again. The thread must not die of one refused write.
            return

    def _pool_missing_thumbnails(self) -> list[tuple[str, Path, str]]:
        """Every **Pool** member with no cached thumbnail, in id order, less those given up on."""
        missing: list[tuple[str, Path, str]] = []
        for row in self._connect().execute(_SELECT_POOL_THUMBNAILS).fetchall():
            wallpaper_id, source_url = str(row["id"]), str(row["thumbnail_url"])
            if wallpaper_id in self._thumbnails_given_up:
                continue
            destination = self.thumbnail_dir / f"{wallpaper_id}{url_suffix(source_url)}"
            if not destination.exists():
                missing.append((wallpaper_id, destination, source_url))
        return missing

    def _thumbnail_cache_full(self) -> bool:
        """Whether the **Thumbnail cache** is at or over `thumbnail_cache_max_mb`."""
        directory = self.thumbnail_dir
        held = sum(cached.size for cached in _cached_thumbnails(directory)) if directory.is_dir() else 0
        return held >= self.get_settings().thumbnail_cache_max_mb * BYTES_IN_A_MEGABYTE

    def _in_pool(self, wallpaper_id: str) -> bool:
        return (
            self._connect().execute("SELECT 1 FROM pool WHERE wallpaper_id = ?", (wallpaper_id,)).fetchone()
            is not None
        )

    def evict_thumbnails(self) -> ThumbnailEviction:
        """Clear out the **Thumbnail cache**, by **Verdict** first and by size only as a backstop (ADR 0009).

        First, every thumbnail with no **Explicit Verdict** standing, not in the **Pool** and not in the live
        **Batch**: nothing will ask for it again. Then, over the cap, the oldest of those still to be shown.
        Neither pass evicts an **Explicit Verdict**, so a cache over the cap on **Favourites** alone says so
        in `over_cap`.
        """
        directory = self.thumbnail_dir
        if not directory.is_dir():
            return _NOTHING_EVICTED
        cached = _cached_thumbnails(directory)
        if not cached:
            return _NOTHING_EVICTED

        decided = decisions.explicitly_decided(self._connect())
        awaiting = self._awaiting_a_verdict()
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

        cap = self.get_settings().thumbnail_cache_max_mb * BYTES_IN_A_MEGABYTE
        # Oldest modified first, the name breaking ties so the order never depends on the listing.
        for thumbnail in sorted(capped, key=lambda cached: (cached.modified_at, cached.path.name)):
            if remaining <= cap:
                break
            thumbnail.path.unlink(missing_ok=True)
            evicted.append(thumbnail.wallpaper_id)
            remaining -= thumbnail.size

        return ThumbnailEviction(evicted=tuple(evicted), remaining_bytes=remaining, over_cap=remaining > cap)

    def _awaiting_a_verdict(self) -> set[str]:
        """The **Pool** and the live **Batch**, which can hold a **Wallpaper** pruned from the **Pool**
        since.
        """
        return {str(row["wallpaper_id"]) for row in self._connect().execute(_AWAITING_A_VERDICT)}

    # -- verdict resolution and history: forwarding to `decisions` until the web layer calls it directly ----

    def resolve_verdicts(self, wallpaper_ids: Sequence[str]) -> dict[str, ResolvedVerdict]:
        """**Verdict resolution** for many **Wallpapers** at once: `decisions.resolve`."""
        return decisions.resolve(self._connect(), wallpaper_ids)

    def edit_verdict(self, wallpaper_id: str, verdict: Verdict) -> HistoryRefused | None:
        """Change a **Wallpaper**'s **Verdict** from **History** by appending an entry with `batch_id` `NULL`.

        An **Ignore** is how **History** withdraws a **Verdict**, un-**Banning** included. An unknown
        **Wallpaper** is refused rather than left to the foreign key. The **Library** is reconciled
        afterwards, outside the transaction.
        """
        recorded_at = self._clock.now()
        with self._write() as write:
            if not _wallpaper_exists(write, wallpaper_id):
                return HistoryRefused(reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER)
            decisions.append(write, {wallpaper_id: verdict}, batch_id=None, at=recorded_at)
        self.reconcile_library()
        return None

    def list_history_rows(self, *, verdict: Verdict | None = None, page: int = 1) -> HistoryPage:
        """One page of **History**: `decisions.history`, with each line's **Wallpaper**."""
        listing = decisions.history(self._connect(), verdict, page)
        return HistoryPage(
            rows=self._history_rows_from(listing.entries),
            page=listing.page,
            pages=listing.pages,
            total=listing.total,
            verdict=verdict,
        )

    def get_history_row(self, wallpaper_id: str) -> HistoryRow | None:
        """One **Wallpaper**'s **History** row, or `None`, for an edit to swap in: what it resolves to, and
        its latest entry's timestamp, which is the deciding one.
        """
        connection = self._connect()
        logged = decisions.entries(connection, wallpaper_id=wallpaper_id)
        if not logged:
            return None
        line = decisions.HistoryEntry(
            wallpaper_id=wallpaper_id,
            resolved=decisions.resolve(connection, [wallpaper_id])[wallpaper_id],
            decided_at=logged[-1].recorded_at,
        )
        return self._history_rows_from([line])[0]

    def _history_rows_from(self, lines: Sequence[decisions.HistoryEntry]) -> tuple[HistoryRow, ...]:
        """Join each line's **Wallpaper** on, the page's in one query."""
        placeholders = ",".join("?" * len(lines))
        rows = self._connect().execute(
            f"SELECT * FROM wallpapers WHERE id IN ({placeholders})", [line.wallpaper_id for line in lines]
        )
        wallpapers = {str(row["id"]): wallpaper_from_row(row) for row in rows}
        return tuple(
            HistoryRow(
                wallpaper=wallpapers[line.wallpaper_id], resolved=line.resolved, latest_at=line.decided_at
            )
            for line in lines
        )

    def list_history(
        self, *, batch_id: str | None = None, wallpaper_id: str | None = None
    ) -> list[DecisionEntry]:
        """The **Decision log**'s raw entries in sequence order: `decisions.entries`."""
        return decisions.entries(self._connect(), batch_id=batch_id, wallpaper_id=wallpaper_id)


def thumbnail_loop(core: CoreService, stop_event: threading.Event) -> None:
    """Wait as the Core service says, take one step, repeat; `thumbnail_step` never raises."""
    while not stop_event.is_set():
        wait = core.thumbnail_wait()
        if wait > 0 and stop_event.wait(wait):
            return
        if stop_event.is_set():
            return
        core.thumbnail_step()


_AWAITING_A_VERDICT = """
SELECT wallpaper_id FROM pool
UNION
SELECT bw.wallpaper_id
FROM batch_wallpapers AS bw
JOIN batches AS b ON b.id = bw.batch_id
WHERE b.submitted_at IS NULL
"""
"""Every **Wallpaper** still to be shown: the **Pool**, plus the live **Batch**."""


def _wallpaper_exists(connection: sqlite3.Connection, wallpaper_id: str) -> bool:
    """Whether this database has ever seen the **Wallpaper**, so an edit is refused rather than an
    `IntegrityError`.
    """
    return connection.execute("SELECT 1 FROM wallpapers WHERE id = ?", (wallpaper_id,)).fetchone() is not None


def _cached_thumbnails(directory: Path) -> list[_CachedThumbnail]:
    """Every file in the **Thumbnail cache**, stat-ed once, keyed by the **Wallpaper** id its name carries.

    A file that vanishes before the stat is skipped: this runs after a submission and must not raise.
    """
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
"""An empty or absent **Thumbnail cache**: nothing to delete and nothing taking up room."""


_SELECT_POOL_THUMBNAILS = """
SELECT w.id, w.thumbnail_url
FROM pool
JOIN wallpapers AS w ON w.id = pool.wallpaper_id
ORDER BY pool.wallpaper_id
"""
"""Every **Pool** member's thumbnail URL, in the order the downloader fetches them."""


def gap_needed(last_fetch: float | None, *, now: float, gap: float = THUMBNAIL_GAP_SECONDS) -> float:
    """Seconds to wait before the next thumbnail fetch, or zero. A gap, not a window."""
    if last_fetch is None:
        return 0.0
    return max(0.0, last_fetch + gap - now)
