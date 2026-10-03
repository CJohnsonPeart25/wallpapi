"""The Core service: the interface between the UI and everything else, with SQLite as an in-process detail."""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

from wallpapi.allocation import ZONE_ORDER, allocate, varied_order
from wallpapi.clock import Clock
from wallpapi.files import write_atomically
from wallpapi.library import LibraryWriter
from wallpapi.model import Clearance, DecisionEntry, Mix, Verdict, Wallpaper, Zone
from wallpapi.ratelimit import CALLS_PER_MINUTE, WINDOW_SECONDS, gap_needed, wait_needed
from wallpapi.rng import SeededRandom
from wallpapi.scoring import classify
from wallpapi.similarity import SimilarityProvider
from wallpapi.wallhaven import RateLimited, SearchPage, ThumbnailUnavailable, Wallhaven

SCHEMA_VERSION = 10

SFW_PURITY = "100"
"""Wallhaven's purity mask: SFW on, sketchy and NSFW off. Fixed; NSFW is what needs an API key."""

SFW_PURITY_NAME = "sfw"
"""What a search *result* calls the same thing. The mask is a query parameter; this is a response field."""

ALL_CATEGORIES = "111"
"""Wallhaven's category mask: general, anime and people, all on. The **Filters** are about shape, not
subject.
"""

IDLE_RECHECK_SECONDS = 30.0
"""How long the refill waits before looking again once the **Pool** is at target: not a spin, not minutes."""

ERROR_BACKOFF_SECONDS = 60.0
"""How long the refill waits after a failed **API call** that named no delay: one whole rate-limit window."""

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

POOL_SOURCE_RANDOM = "random"
POOL_SOURCE_LIKE = "like"
"""How a **Pool** member got there: a random walk, or a like: search on a **Favourite**."""

LIKE_QUERY_PREFIX = "like:"
"""Wallhaven's spelling of "wallpapers similar to this one", sent as the `q` of a search."""

LIKE_SORTING = "relevance"
"""How a like: search is sorted: most similar first, because the walk is capped and the tail is weak."""

LIKE_PAGES_PER_FAVOURITE = 3
"""How far a like: walk goes before the next **Favourite**'s turn: 72 **Wallpapers**, and the tail is weak."""

MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 64
"""The accepted batch size range, inclusive: a **Batch** of none has no way off it, and 64 is as many as
anyone can judge at once.
"""

DEFAULT_BATCH_SIZE = 8

MIN_POOL_TARGET_SIZE = 1
MAX_POOL_TARGET_SIZE = 20_000
"""The accepted **Pool** target size range. Every whole-**Pool** operation is linear in it, so there is a
ceiling rather than a **Pool** that grows and slows for ever.
"""

DEFAULT_POOL_TARGET_SIZE = 500
"""Enough to draw from, not a backlog (ADR 0016): every submission retires what it showed, and smaller is
cheaper on every **Batch** minted.
"""

SUPERSEDED_POOL_TARGET_SIZE = 2000
"""The target ADR 0005 seeded, kept only because migration 10 has to recognise it."""

MAX_FILTER_PIXELS = 30_000
"""The largest minimum resolution accepted, per axis. Anything above it is a typo and an empty **Pool**."""

DEFAULT_MIN_WIDTH = 2560
DEFAULT_MIN_HEIGHT = 1440
"""1440p as a minimum (`atleast`): native 1440p and larger, not upscaled 1080p."""

DEFAULT_ALLOWED_RATIOS = ("16x9", "16x10", "21x9")
"""The shapes a desktop monitor actually is: widescreen, 16:10 and ultrawide."""

DEFAULT_MIN_FAVOURITES = 10
"""Skips the long tail nobody has looked at. Applied locally: Wallhaven's search has no parameter for it."""

SUPERSEDED_SIMILARITY_RADIUS = 0.5
"""The radius ADR 0007 seeded, kept only because migration 9 has to recognise it."""

DEFAULT_SIMILARITY_RADIUS = 0.15
"""How far a decided **Wallpaper**'s influence reaches, as a distance in `[0, 1]`.

Not the accuracy-maximising value, deliberately: 0.2 got the sign right more often but left 1% of the **Pool**
**Unknown**, and **Unknown** is what **Explore** draws from. See ADR 0013.
"""

DEFAULT_SIMILARITY_DECAY = 4.0
"""How fast that influence fades, as the rate in `exp(-decay * distance)`. Zero is allowed: no fading."""

MAX_SIMILARITY_DECAY = 50.0
"""`exp(-50 * d)` is under `1e-21` at a hundredth of the range; anything higher is a **Pool** of
**Unknowns**.
"""

DEFAULT_THUMBNAIL_CACHE_MAX_MB = 500
"""The **Thumbnail cache**'s size cap, a backstop behind eviction by **Verdict** (ADR 0009).

At about 23KiB a thumbnail it never fires in normal running. The downloader holds off at the cap rather than
churn against it (ADR 0017).
"""

BYTES_IN_A_MEGABYTE = 1024 * 1024
"""Mebibytes, because matching what Explorer shows matters more than matching SI."""

HISTORY_PAGE_SIZE = 100
"""Rows on one page of **History**: it grows by thousands of **Ignores** a week and needs *a* bound."""

WALLHAVEN_RATIOS = frozenset(
    {"16x9", "16x10", "21x9", "32x9", "48x9", "9x16", "10x16", "9x18", "1x1", "3x2", "4x3", "5x4"}
)
"""The `ratios=` values Wallhaven accepts. An unrecognised one is not an error there, just a different
search.
"""

RATIO_TOLERANCE = 0.08
"""How far a **Wallpaper**'s own width/height may sit from a named ratio and still count as it.

Wallhaven buckets: a 3440x1440 is 2.39 and served under `21x9`, which is 2.33. Generous on purpose, and still
under half the gap between `16x9` (1.78) and `16x10` (1.60).
"""

EXPLORE_MIX = Mix(name="explore", unknown=75, banger=20, dud=5)
REFINE_MIX = Mix(name="refine", unknown=25, banger=70, dud=5)
DEFAULT_MIXES = (EXPLORE_MIX, REFINE_MIX)
"""The two **Mixes** the spec names: the seed for an empty database and the fallback for an emptied table.

The 5% of **Duds** is deliberate: a **Dud** can stop being one the moment something near it is **Favourited**,
and never will if it is never shown.
"""

DEFAULT_ACTIVE_MIX = EXPLORE_MIX.name
"""**Explore**, because an empty **Decision log** has no **Bangers** to refine towards."""

MIX_TOTAL = 100
"""What a **Mix** must sum to, exactly: a **Mix** summing to 99 would make the leftover roll systematic."""

MAX_MIX_NAME_LENGTH = 40
"""How long a **Mix** name may be: it is a button beside the others, and must not push them off the row."""

UNDELETABLE_MIXES = frozenset(mix.name for mix in DEFAULT_MIXES)
"""**Explore** and **Refine**: editable, never deleted, so `CONTEXT.md`'s two terms always have a **Mix**."""

_BATCH_SIZE = "batch_size"
_LIBRARY_PATH = "library_path"
_POOL_TARGET_SIZE = "pool_target_size"
_MIN_WIDTH = "min_width"
_MIN_HEIGHT = "min_height"
_ALLOWED_RATIOS = "allowed_ratios"
_MIN_FAVOURITES = "min_favourites"
_SIMILARITY_RADIUS = "similarity_radius"
_SIMILARITY_DECAY = "similarity_decay"
_THUMBNAIL_CACHE_MAX_MB = "thumbnail_cache_max_mb"
_ACTIVE_MIX = "active_mix"
"""The `settings` keys. One row per key, with `Settings` as the typed view over them."""


def _default_library_path() -> Path:
    """Under Pictures, where Windows' slideshow settings start, in a subfolder of its own."""
    return Path.home() / "Pictures" / "wallpapi"


def _defaults() -> dict[str, str]:
    """Every setting's seeded value, as stored: what migration 3 seeds and `get_settings` falls back to."""
    return {
        _BATCH_SIZE: str(DEFAULT_BATCH_SIZE),
        _LIBRARY_PATH: str(_default_library_path()),
        _POOL_TARGET_SIZE: str(DEFAULT_POOL_TARGET_SIZE),
        _MIN_WIDTH: str(DEFAULT_MIN_WIDTH),
        _MIN_HEIGHT: str(DEFAULT_MIN_HEIGHT),
        _ALLOWED_RATIOS: ",".join(DEFAULT_ALLOWED_RATIOS),
        _MIN_FAVOURITES: str(DEFAULT_MIN_FAVOURITES),
        _SIMILARITY_RADIUS: str(DEFAULT_SIMILARITY_RADIUS),
        _SIMILARITY_DECAY: str(DEFAULT_SIMILARITY_DECAY),
        _THUMBNAIL_CACHE_MAX_MB: str(DEFAULT_THUMBNAIL_CACHE_MAX_MB),
        _ACTIVE_MIX: DEFAULT_ACTIVE_MIX,
    }


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything configurable, as one typed view over one row per key."""

    batch_size: int
    library_path: Path
    pool_target_size: int
    min_width: int
    min_height: int
    allowed_ratios: tuple[str, ...]
    min_favourites: int
    similarity_radius: float
    similarity_decay: float
    thumbnail_cache_max_mb: int
    active_mix: str
    """Which **Mix** the next **Batch** is built from, by name.

    Only the name, so `get_settings` stays one query that never refuses; `CoreService.active_mix()` resolves
    it.
    """

    @property
    def atleast(self) -> str:
        """The minimum resolution in Wallhaven's `WxH` spelling."""
        return f"{self.min_width}x{self.min_height}"

    @property
    def ratios(self) -> str:
        """The allowed ratios in Wallhaven's comma-separated spelling."""
        return ",".join(self.allowed_ratios)


@dataclass(frozen=True)
class SettingsRefused:
    """The update did not happen, and this is why. A result, so the page has one error branch."""

    class Reason(StrEnum):
        BATCH_SIZE_NOT_A_NUMBER = "batch_size_not_a_number"
        BATCH_SIZE_OUT_OF_RANGE = "batch_size_out_of_range"
        LIBRARY_PATH_EMPTY = "library_path_empty"
        LIBRARY_PATH_NOT_ABSOLUTE = "library_path_not_absolute"
        POOL_TARGET_SIZE_INVALID = "pool_target_size_invalid"
        MIN_WIDTH_INVALID = "min_width_invalid"
        MIN_HEIGHT_INVALID = "min_height_invalid"
        ALLOWED_RATIOS_INVALID = "allowed_ratios_invalid"
        MIN_FAVOURITES_INVALID = "min_favourites_invalid"
        SIMILARITY_RADIUS_INVALID = "similarity_radius_invalid"
        SIMILARITY_DECAY_INVALID = "similarity_decay_invalid"
        """One reason per **Filter** field: for these, both mistakes have the same one-sentence answer."""

        THUMBNAIL_CACHE_MAX_MB_INVALID = "thumbnail_cache_max_mb_invalid"

        ACTIVE_MIX_UNKNOWN = "active_mix_unknown"
        """No stored **Mix** goes by that name."""

        MIX_NAME_INVALID = "mix_name_invalid"
        MIX_PERCENTAGES_INVALID = "mix_percentages_invalid"
        """The two ways `validated_mix` refuses, so the form can say which field is wrong."""

        MIX_UNKNOWN = "mix_unknown"
        """No stored **Mix** goes by that name, so there is nothing to delete."""

        MIX_IN_USE = "mix_in_use"
        """That **Mix** is the active one; switch away first."""

        MIX_NOT_DELETABLE = "mix_not_deletable"
        """**Explore** and **Refine** are editable and permanent."""

    reason: Reason


@dataclass(frozen=True, slots=True)
class Batch:
    """The **Wallpapers** shown at once, with the identity the submission quotes back.

    `drafts` holds only marked **Wallpapers**: absence is an **Ignore**. `zones` is the **Zone** each was
    drawn from at mint time, never recomputed; absent for a **Batch** minted before **Zones** existed.
    """

    id: str
    size: int
    created_at: dt.datetime
    wallpapers: tuple[Wallpaper, ...]
    drafts: Mapping[str, Verdict]
    zones: Mapping[str, Zone]


@dataclass(frozen=True)
class BatchUnavailable:
    """No **Batch** could be built: the **Pool** is empty, and the page needs to say whether that is waiting
    or Wallhaven being unreachable.
    """

    class Reason(StrEnum):
        POOL_EMPTY = "pool_empty"
        """The **Pool** holds nothing the user has not **Banned**, and the refill has not failed."""

        WALLHAVEN_UNREACHABLE = "wallhaven_unreachable"
        """The **Pool** is empty and the last refill attempt failed. `error` says how."""

    reason: Reason
    error: str | None = None
    error_at: dt.datetime | None = None


class RefillStrategy(StrEnum):
    """Which search a refill step makes, and so the `source` it tags what it admits with."""

    RANDOM = POOL_SOURCE_RANDOM
    LIKE = POOL_SOURCE_LIKE


@dataclass(frozen=True, slots=True)
class RefillStatus:
    """What the background **Pool** refill is doing, for the indicator on the **Batch** page."""

    pool_size: int
    target_size: int
    running: bool
    last_run_at: dt.datetime | None
    last_error: str | None
    last_error_at: dt.datetime | None
    last_strategy: RefillStrategy | None
    """Which search the last step made, or `None`. A stalled like: rotation means no **Favourites** yet."""

    @property
    def at_target(self) -> bool:
        """Whether the refill is idling rather than spending its budget."""
        return self.pool_size >= self.target_size


@dataclass(frozen=True, slots=True)
class ResolvedVerdict:
    """What one **Wallpaper**'s **Decision log** entries come to: the `verdict` to show and the `value` to
    score.
    """

    verdict: Verdict | None
    value: int


@dataclass(frozen=True, slots=True)
class ScoredWallpaper:
    """One **Pool** **Wallpaper**, its **Score** and its **Zone**. Derived on every call, never stored."""

    wallpaper: Wallpaper
    score: float
    zone: Zone


@dataclass(frozen=True, slots=True)
class LibraryReconciliation:
    """What one **Library** reconciliation did. It runs after commit and cannot raise, so `failed` is the
    report.
    """

    written: tuple[str, ...]
    removed: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FavouriteDownload:
    """What one "download all **Favourites**" pass did. It never deletes; the settings page says all three."""

    written: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]


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


@dataclass(frozen=True)
class SubmissionRefused:
    """The submission did not happen, and this is why. Never a silent no-op — two tabs is a real case."""

    class Reason(StrEnum):
        UNKNOWN_BATCH = "unknown_batch"
        ALREADY_SUBMITTED = "already_submitted"

    reason: Reason


class _Connections(threading.local):
    """One SQLite connection per thread: the threadpool hands each request a different one."""

    connection: sqlite3.Connection | None = None


class CoreService:
    def __init__(
        self,
        *,
        db_path: Path,
        wallhaven: Wallhaven,
        library: LibraryWriter,
        similarity: SimilarityProvider,
        random_source: SeededRandom,
        clock: Clock,
    ) -> None:
        self._db_path = db_path
        self._wallhaven = wallhaven
        self._library = library
        self._similarity = similarity
        self._random = random_source
        self._clock = clock
        self._connections = _Connections()

        # The refill's own state, in memory: a walk half-finished at shutdown is worth nothing afterwards.
        # Locked because the refill thread writes it and request threads read it.
        self._refill_lock = threading.Lock()
        self._api_call_times: deque[float] = deque(maxlen=CALLS_PER_MINUTE)
        """The **API call** timestamps still inside the limiter's window.

        Trimmed by age on every append, and capped by `maxlen` as well: only the cap holds when the clock does
        not move.
        """
        self._walk_seed: str | None = None
        self._walk_page = 1

        # The like: walk, kept apart from the random one: they interleave, and would clobber a shared place.
        self._like_subject: str | None = None
        """The **Favourite** whose lookalikes are being walked, or `None` between walks."""
        self._like_seed: str | None = None
        self._like_page = 1
        self._like_walked: set[str] = set()
        """The **Favourites** already walked this cycle, so every one has a turn before any has a second."""
        self._last_strategy: RefillStrategy | None = None

        self._retry_not_before: float | None = None
        self._refill_last_run: dt.datetime | None = None
        self._refill_last_error: str | None = None
        self._refill_last_error_at: dt.datetime | None = None
        self._refill_thread_running = False

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

        self._migrate()

    # -- storage ---------------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = self._connections.connection
        if connection is None:
            connection = sqlite3.connect(self._db_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            # Per-connection, and so re-applied every time: neither survives into a new connection.
            connection.execute("PRAGMA foreign_keys = ON")
            self._connections.connection = connection
        return connection

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        """A write transaction, opened with an explicit `BEGIN IMMEDIATE`.

        A transaction that starts as a reader and upgrades to a writer gets `SQLITE_BUSY_SNAPSHOT` with the
        busy handler skipped, so `busy_timeout` would not save it.
        """
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def _migrate(self) -> None:
        """Apply the numbered steps this database has not seen. A second Core service over one file finds
        nothing.
        """
        connection = self._connect()
        applied = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if applied >= SCHEMA_VERSION:
            return
        # Persists in the file, so it is set once here rather than per connection. Cannot run in a
        # transaction.
        connection.execute("PRAGMA journal_mode = WAL")
        with self._write() as write:
            if applied < 1:
                for statement in _MIGRATION_1:
                    write.execute(statement)
            if applied < 2:
                for statement in _MIGRATION_2:
                    write.execute(statement)
            if applied < 3:
                for statement, parameters in _migration_3():
                    write.execute(statement, parameters)
            if applied < 4:
                for statement in _MIGRATION_4:
                    write.execute(statement)
            if applied < 5:
                for statement, parameters in _migration_5():
                    write.execute(statement, parameters)
            if applied < 6:
                for statement, parameters in _migration_6():
                    write.execute(statement, parameters)
            if applied < 7:
                for statement, mix_parameters in _migration_7():
                    write.execute(statement, mix_parameters)
            if applied < 8:
                for statement, parameters in _migration_8():
                    write.execute(statement, parameters)
            if applied < 9:
                for statement, parameters in _migration_9():
                    write.execute(statement, parameters)
            if applied < 10:
                for statement, parameters in _migration_10():
                    write.execute(statement, parameters)
            write.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- settings --------------------------------------------------------------------------------------

    def get_settings(self) -> Settings:
        """Everything configurable. Never refuses: a hand-edited bad row falls back to its default, so the
        settings page still opens to fix it.
        """
        stored = {
            str(row["key"]): str(row["value"])
            for row in self._connect().execute("SELECT key, value FROM settings")
        }
        defaults = _defaults()

        def stored_or_seeded(key: str) -> str:
            return stored.get(key, defaults[key])

        def or_default[T](validated: T | SettingsRefused.Reason, fallback: T) -> T:
            return fallback if isinstance(validated, SettingsRefused.Reason) else validated

        return Settings(
            batch_size=or_default(_validated_batch_size(stored_or_seeded(_BATCH_SIZE)), DEFAULT_BATCH_SIZE),
            library_path=or_default(
                _validated_library_path(stored_or_seeded(_LIBRARY_PATH)), _default_library_path()
            ),
            pool_target_size=or_default(
                _validated_pool_target_size(stored_or_seeded(_POOL_TARGET_SIZE)),
                DEFAULT_POOL_TARGET_SIZE,
            ),
            min_width=or_default(_validated_min_width(stored_or_seeded(_MIN_WIDTH)), DEFAULT_MIN_WIDTH),
            min_height=or_default(_validated_min_height(stored_or_seeded(_MIN_HEIGHT)), DEFAULT_MIN_HEIGHT),
            allowed_ratios=or_default(
                _validated_allowed_ratios(stored_or_seeded(_ALLOWED_RATIOS)), DEFAULT_ALLOWED_RATIOS
            ),
            min_favourites=or_default(
                _validated_min_favourites(stored_or_seeded(_MIN_FAVOURITES)), DEFAULT_MIN_FAVOURITES
            ),
            similarity_radius=or_default(
                _validated_similarity_radius(stored_or_seeded(_SIMILARITY_RADIUS)),
                DEFAULT_SIMILARITY_RADIUS,
            ),
            similarity_decay=or_default(
                _validated_similarity_decay(stored_or_seeded(_SIMILARITY_DECAY)), DEFAULT_SIMILARITY_DECAY
            ),
            thumbnail_cache_max_mb=or_default(
                _validated_thumbnail_cache_max_mb(stored_or_seeded(_THUMBNAIL_CACHE_MAX_MB)),
                DEFAULT_THUMBNAIL_CACHE_MAX_MB,
            ),
            # Not checked against `mixes`: this view never refuses, and `active_mix()` is where it falls back.
            active_mix=stored_or_seeded(_ACTIVE_MIX).strip() or DEFAULT_ACTIVE_MIX,
        )

    def update_settings(
        self,
        *,
        batch_size: int | str | None = None,
        library_path: Path | str | None = None,
        pool_target_size: int | str | None = None,
        min_width: int | str | None = None,
        min_height: int | str | None = None,
        allowed_ratios: Sequence[str] | str | None = None,
        min_favourites: int | str | None = None,
        similarity_radius: float | str | None = None,
        similarity_decay: float | str | None = None,
        thumbnail_cache_max_mb: int | str | None = None,
        active_mix: str | None = None,
    ) -> Settings | SettingsRefused:
        """Validate and persist the settings named, in one write transaction; `None` leaves a field alone.

        Keywords rather than a whole `Settings`, so a form that renders half the settings cannot reset the
        other half, and there is no read-then-write. Everything is validated before anything is written.
        """
        changes: list[tuple[str, str]] = []
        if batch_size is not None:
            validated_size = _validated_batch_size(batch_size)
            if isinstance(validated_size, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_size)
            changes.append((_BATCH_SIZE, str(validated_size)))
        if library_path is not None:
            validated_path = _validated_library_path(library_path)
            if isinstance(validated_path, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_path)
            # Stored, not created: the writer creates the folder on its first write, so a path typed and
            # undone leaves nothing behind.
            changes.append((_LIBRARY_PATH, str(validated_path)))
        if pool_target_size is not None:
            validated_target = _validated_pool_target_size(pool_target_size)
            if isinstance(validated_target, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_target)
            changes.append((_POOL_TARGET_SIZE, str(validated_target)))
        if min_width is not None:
            validated_width = _validated_min_width(min_width)
            if isinstance(validated_width, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_width)
            changes.append((_MIN_WIDTH, str(validated_width)))
        if min_height is not None:
            validated_height = _validated_min_height(min_height)
            if isinstance(validated_height, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_height)
            changes.append((_MIN_HEIGHT, str(validated_height)))
        if allowed_ratios is not None:
            validated_ratios = _validated_allowed_ratios(allowed_ratios)
            if isinstance(validated_ratios, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_ratios)
            changes.append((_ALLOWED_RATIOS, ",".join(validated_ratios)))
        if min_favourites is not None:
            validated_favourites = _validated_min_favourites(min_favourites)
            if isinstance(validated_favourites, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_favourites)
            changes.append((_MIN_FAVOURITES, str(validated_favourites)))
        if similarity_radius is not None:
            validated_radius = _validated_similarity_radius(similarity_radius)
            if isinstance(validated_radius, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_radius)
            changes.append((_SIMILARITY_RADIUS, str(validated_radius)))
        if similarity_decay is not None:
            validated_decay = _validated_similarity_decay(similarity_decay)
            if isinstance(validated_decay, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_decay)
            changes.append((_SIMILARITY_DECAY, str(validated_decay)))
        if thumbnail_cache_max_mb is not None:
            validated_cap = _validated_thumbnail_cache_max_mb(thumbnail_cache_max_mb)
            if isinstance(validated_cap, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_cap)
            changes.append((_THUMBNAIL_CACHE_MAX_MB, str(validated_cap)))
        if active_mix is not None:
            # Checked against another table, before the write transaction: a second tab deleting this **Mix**
            # meanwhile leaves `active_mix` naming nothing, and `active_mix()` falls back.
            if active_mix.strip() not in {mix.name for mix in self.list_mixes()}:
                return SettingsRefused(reason=SettingsRefused.Reason.ACTIVE_MIX_UNKNOWN)
            changes.append((_ACTIVE_MIX, active_mix.strip()))

        with self._write() as write:
            write.executemany(_UPSERT_SETTING, changes)
            # Read inside the transaction, so what comes back is what this write put there.
            updated = self.get_settings()
            self._prune_pool(write, updated)
            return updated

    # -- mixes -----------------------------------------------------------------------------------------

    def list_mixes(self) -> tuple[Mix, ...]:
        """Every stored **Mix**, by name, or the seeded pair if there are none.

        A hand-edited row that `validated_mix` refuses is dropped rather than offered.
        """
        rows = self._connect().execute("SELECT name, unknown, banger, dud FROM mixes ORDER BY name")
        stored = [
            mix
            for mix in (
                validated_mix(str(row["name"]), unknown=row["unknown"], banger=row["banger"], dud=row["dud"])
                for row in rows.fetchall()
            )
            if isinstance(mix, Mix)
        ]
        return tuple(stored) if stored else DEFAULT_MIXES

    def active_mix(self) -> Mix:
        """The **Mix** the next **Batch** is built from, or the first there is if the stored name matches
        none.
        """
        mixes = self.list_mixes()
        name = self.get_settings().active_mix
        return next((mix for mix in mixes if mix.name == name), mixes[0])

    def save_mix(
        self, name: str, *, unknown: int | str, banger: int | str, dud: int | str
    ) -> Mix | SettingsRefused:
        """Store a **Mix** under that name, replacing any already there. Applies to the next **Batch**, not
        this one.
        """
        mix = validated_mix(name, unknown=unknown, banger=banger, dud=dud)
        if isinstance(mix, SettingsRefused.Reason):
            return SettingsRefused(reason=mix)
        with self._write() as write:
            write.execute(_UPSERT_MIX, (mix.name, mix.unknown, mix.banger, mix.dud))
        return mix

    def delete_mix(self, name: str) -> SettingsRefused | None:
        """Remove a **Mix**, or say why it stays: unknown, then permanent, then in use, in that order."""
        trimmed = name.strip()
        if trimmed not in {mix.name for mix in self.list_mixes()}:
            return SettingsRefused(reason=SettingsRefused.Reason.MIX_UNKNOWN)
        if trimmed in UNDELETABLE_MIXES:
            return SettingsRefused(reason=SettingsRefused.Reason.MIX_NOT_DELETABLE)
        if trimmed == self.get_settings().active_mix:
            return SettingsRefused(reason=SettingsRefused.Reason.MIX_IN_USE)
        with self._write() as write:
            write.execute(_DELETE_MIX, (trimmed,))
        return None

    # -- batches ---------------------------------------------------------------------------------------

    def get_next_batch(self) -> Batch | BatchUnavailable:
        """The **Batch** waiting to be decided on, sampling the **Pool** only if there isn't one. No **API
        call**.

        The **Zone** recorded per **Wallpaper** is where it was drawn from, never the slot's: after a
        shortfall the two differ, and the tile says where the **Wallpaper** came from.

        Pre-marking: a chosen **Wallpaper** whose resolved **Verdict** is explicit gets a **Draft Batch** row,
        so leaving it alone records it again (ADR 0015). Dormant while nothing decided is in the **Pool** (ADR
        0016).
        """
        live = self._live_batch()
        if live is not None:
            return live

        size = self.get_settings().batch_size
        classified = self.classify_pool()
        if not classified:
            return self._nothing_to_show()

        chosen = self._choose(classified, size)
        created_at = self._clock.now()
        batch_id = uuid4().hex

        with self._write() as write:
            # Re-read under the write lock (ADR 0002): two tabs opened at once must not each mint a **Batch**.
            contended = _load_live_batch(write)
            if contended is not None:
                return contended
            write.execute(
                "INSERT INTO batches (id, created_at, size) VALUES (?, ?, ?)",
                (batch_id, created_at.isoformat(), len(chosen)),
            )
            write.executemany(
                "INSERT INTO batch_wallpapers (batch_id, wallpaper_id, position, zone) VALUES (?, ?, ?, ?)",
                [
                    (batch_id, scored.wallpaper.id, position, scored.zone.value)
                    for position, scored in enumerate(chosen)
                ],
            )
            # Resolved under the write lock, on this thread's one connection, so a **History** edit cannot
            # land between reading a **Verdict** and pre-filling it.
            resolved = self.resolve_verdicts([scored.wallpaper.id for scored in chosen])
            drafts = {
                wallpaper_id: standing.verdict
                for wallpaper_id, standing in resolved.items()
                if standing.verdict is not None and _is_explicit(standing)
            }
            write.executemany(
                "INSERT INTO draft_batch (batch_id, wallpaper_id, verdict) VALUES (?, ?, ?)",
                [(batch_id, wallpaper_id, verdict.value) for wallpaper_id, verdict in drafts.items()],
            )

        return Batch(
            id=batch_id,
            size=len(chosen),
            created_at=created_at,
            wallpapers=tuple(scored.wallpaper for scored in chosen),
            drafts=drafts,
            zones={scored.wallpaper.id: scored.zone for scored in chosen},
        )

    def _choose(self, classified: Sequence[ScoredWallpaper], size: int) -> list[ScoredWallpaper]:
        """Which of the classified **Pool** a **Batch** shows: slots by **Mix**, each **Zone** in its draw
        order, and any shortfall refilled in `ZONE_ORDER`.

        Shuffled at the end, so a tile's **Zone** cannot be read from its position.
        """
        slots = allocate(self.active_mix(), size, self._random)
        orders = {zone: self._draw_order(zone, classified, slots[zone]) for zone in ZONE_ORDER}
        taken = dict.fromkeys(ZONE_ORDER, 0)
        chosen: list[ScoredWallpaper] = []

        def take(zone: Zone, count: int) -> int:
            """Up to `count` more from `zone`, in its draw order. Returns how many there were."""
            available = orders[zone][taken[zone] : taken[zone] + count]
            taken[zone] += len(available)
            chosen.extend(available)
            return len(available)

        shortfall = sum(slots[zone] - take(zone, slots[zone]) for zone in ZONE_ORDER)
        # One pass is enough: after it every **Zone** is exhausted or the shortfall is met.
        for zone in ZONE_ORDER:
            if shortfall <= 0:
                break
            shortfall -= take(zone, shortfall)
        return self._random.sample(chosen, len(chosen))

    def _draw_order(
        self, zone: Zone, classified: Sequence[ScoredWallpaper], slots: int
    ) -> list[ScoredWallpaper]:
        """The order one **Zone** gives its **Wallpapers** up in, best first.

        **Bangers** by **Score** over a random order, so ties break by the seed. **Duds** stay random.
        **Unknowns** one per look-alike group first (ADR 0018), or random when the provider has no `vectors`.
        """
        members = [scored for scored in classified if scored.zone is zone]
        ordered = self._random.sample(members, len(members))
        if zone is Zone.BANGER:
            ordered.sort(key=lambda scored: scored.score, reverse=True)
        if zone is Zone.UNKNOWN:
            vectors = self._similarity.vectors([scored.wallpaper for scored in ordered])
            if vectors is not None:
                favourites = [scored.wallpaper.favourites for scored in ordered]
                ordered = [ordered[i] for i in varied_order(vectors, favourites, slots, self._random)]
        return ordered

    # -- scoring and zones -----------------------------------------------------------------------------

    def classify_pool(self) -> tuple[ScoredWallpaper, ...]:
        """Every **Pool** **Wallpaper** that is not **Banned**, with its **Score** and **Zone**, in one call.

        The decided columns are every **Wallpaper** with a non-zero resolved value, in the **Pool** or not; a
        **Ban** is a column like any other, but never a row.
        """
        pool = self._pool_wallpapers()
        if not pool:
            return ()
        judged = [_wallpaper_from_row(row) for row in self._connect().execute(_SELECT_DECIDED_WALLPAPERS)]
        resolved = self.resolve_verdicts([w.id for w in pool] + [w.id for w in judged])

        candidates = [w for w in pool if resolved[w.id].verdict is not Verdict.BAN]
        decided = [w for w in judged if resolved[w.id].value != 0]
        if not candidates:
            return ()

        settings = self.get_settings()
        classification = classify(
            self._similarity.similarities(candidates, decided),
            [resolved[w.id].value for w in decided],
            radius=settings.similarity_radius,
            decay=settings.similarity_decay,
        )
        return tuple(
            ScoredWallpaper(wallpaper=wallpaper, score=float(score), zone=zone)
            for wallpaper, score, zone in zip(
                candidates, classification.scores, classification.zones, strict=True
            )
        )

    def _nothing_to_show(self) -> BatchUnavailable:
        """Why the **Pool** had nothing. A recorded refill failure outranks "nothing yet"."""
        with self._refill_lock:
            error, error_at = self._refill_last_error, self._refill_last_error_at
        if error is not None:
            return BatchUnavailable(
                reason=BatchUnavailable.Reason.WALLHAVEN_UNREACHABLE, error=error, error_at=error_at
            )
        return BatchUnavailable(reason=BatchUnavailable.Reason.POOL_EMPTY)

    # -- the Similarity provider's own upkeep ------------------------------------------------------------

    def similarity_notice(self) -> str | None:
        """One line for the page when the **Similarity provider** is not at full strength.

        Handed the **Pool** less what the thumbnail downloader gave up on, so the line clears once everything
        that can be embedded has been.
        """
        given_up = self._thumbnails_given_up
        return self._similarity.notice([w for w in self._pool_wallpapers() if w.id not in given_up])

    def similarity_step(self, stop_event: threading.Event) -> float:
        """One step of the **Similarity provider**'s upkeep, on its background thread only; seconds to the
        next.
        """
        return self._similarity.catch_up(self.thumbnail_dir, stop_event)

    # -- the Pool and its refill -------------------------------------------------------------------------

    def refill_status(self) -> RefillStatus:
        """What the refill is doing, for the indicator on the **Batch** page."""
        with self._refill_lock:
            return RefillStatus(
                pool_size=self._pool_size(),
                target_size=self.get_settings().pool_target_size,
                running=self._refill_thread_running,
                last_run_at=self._refill_last_run,
                last_error=self._refill_last_error,
                last_error_at=self._refill_last_error_at,
                last_strategy=self._last_strategy,
            )

    def refill_wait(self) -> float:
        """Seconds before the next `refill_step`: the longest of idling at target, the rate limiter and a
        back-off.
        """
        now = self._clock.monotonic()
        if self._pool_size() >= self.get_settings().pool_target_size:
            return IDLE_RECHECK_SECONDS
        with self._refill_lock:
            limited = wait_needed(self._api_call_times, now=now)
            backing_off = 0.0 if self._retry_not_before is None else self._retry_not_before - now
        return max(limited, backing_off, 0.0)

    def refill_step(self) -> None:
        """One step of the refill: at most one **API call**, and never an exception, because the thread must
        not die.

        The random and like: strategies take strict turns; with no **Favourites** every step is random. A
        failure is recorded, the walk keeps its place, and the caller backs off.
        """
        settings = self.get_settings()
        self._mark_refill_run()
        if self._pool_size() >= settings.pool_target_size:
            # At target: the random walk is over, and the next starts from a fresh seed.
            self._reset_walk()
            return

        favourites = self._favourites()
        with self._refill_lock:
            strategy = _alternated(self._last_strategy, has_favourites=bool(favourites))
            self._last_strategy = strategy
            if strategy is RefillStrategy.LIKE:
                subject = self._take_up_a_like_walk(favourites)
                seed, page_number = self._like_seed, self._like_page
            else:
                subject = None
                seed, page_number = self._walk_seed, self._walk_page
            self._record_api_call(self._clock.monotonic())
        try:
            page = self._wallhaven.search(
                sorting="random" if subject is None else LIKE_SORTING,
                query=None if subject is None else f"{LIKE_QUERY_PREFIX}{subject}",
                purity=SFW_PURITY,
                categories=ALL_CATEGORIES,
                page=page_number,
                seed=seed,
                atleast=settings.atleast,
                ratios=settings.ratios,
            )
        except RateLimited as limited:
            # Wallhaven's own answer beats the default: it knows when it will start answering again.
            self._record_refill_failure(limited, limited.retry_after or ERROR_BACKOFF_SECONDS)
            return
        except Exception as failure:
            # Deliberately everything: the protocol names only `RateLimited`, and one unexpected type must
            # not kill the thread.
            self._record_refill_failure(failure, ERROR_BACKOFF_SECONDS)
            return

        self._admit_to_pool(page.wallpapers, settings, source=strategy)
        if strategy is RefillStrategy.LIKE:
            self._advance_like_walk(page)
        else:
            self._advance_walk(page)
        with self._refill_lock:
            self._retry_not_before = None
            self._refill_last_error = None
            self._refill_last_error_at = None

    @contextmanager
    def refill_running(self) -> Generator[None]:
        """Marks the refill as running while the thread's loop is inside this, and clears it however the loop
        ends.
        """
        with self._refill_lock:
            self._refill_thread_running = True
        try:
            yield
        finally:
            with self._refill_lock:
                self._refill_thread_running = False

    def _record_api_call(self, at: float) -> None:
        """Note an **API call** and drop those aged out of the window, so `wait_needed` stays a pure function.
        Lock held.
        """
        while self._api_call_times and at - self._api_call_times[0] >= WINDOW_SECONDS:
            self._api_call_times.popleft()
        self._api_call_times.append(at)

    def _mark_refill_run(self) -> None:
        with self._refill_lock:
            self._refill_last_run = self._clock.now()

    def _record_refill_failure(self, failure: Exception, backoff: float) -> None:
        """Remember why the last **API call** failed, for the page, and how long to leave Wallhaven alone."""
        with self._refill_lock:
            self._refill_last_error = str(failure) or type(failure).__name__
            self._refill_last_error_at = self._clock.now()
            self._retry_not_before = self._clock.monotonic() + backoff

    def _advance_walk(self, page: SearchPage) -> None:
        """Carry `meta.seed` to the next page of this walk, or start a fresh walk on an empty page."""
        with self._refill_lock:
            if not page.wallpapers:
                self._walk_seed, self._walk_page = None, 1
                return
            self._walk_seed = page.seed or self._walk_seed
            self._walk_page += 1

    def _reset_walk(self) -> None:
        """Forget where the random walk had got to. The like: walk keeps its place: `like:<id>` has no seed
        trap.
        """
        with self._refill_lock:
            self._walk_seed, self._walk_page = None, 1

    def _favourites(self) -> list[str]:
        """Every **Wallpaper** whose *resolved* **Verdict** is **Favourite**, ordered so a seeded draw is
        repeatable.
        """
        rows = self._connect().execute(_FAVOURITED_AT_LEAST_ONCE, (Verdict.FAVOURITE.value,)).fetchall()
        candidates = [str(row["wallpaper_id"]) for row in rows]
        resolved = self.resolve_verdicts(candidates)
        return [c for c in candidates if resolved[c].verdict is Verdict.FAVOURITE]

    def _take_up_a_like_walk(self, favourites: Sequence[str]) -> str:
        """The **Favourite** whose lookalikes the next like: search asks for. Lock held.

        Carries on with the walk in progress while its subject is still a **Favourite**; otherwise starts on
        one that has not had a turn this cycle, so every **Favourite** gets one.
        """
        current = self._like_subject
        if current is not None and current in favourites:
            return current
        self._like_walked.intersection_update(favourites)
        remaining = [f for f in favourites if f not in self._like_walked]
        if not remaining:
            self._like_walked.clear()
            remaining = list(favourites)
        chosen = self._random.sample(remaining, 1)[0]
        self._like_subject, self._like_seed, self._like_page = chosen, None, 1
        return chosen

    def _advance_like_walk(self, page: SearchPage) -> None:
        """Page on through one **Favourite**'s lookalikes until an empty page or
        `LIKE_PAGES_PER_FAVOURITE`.
        """
        with self._refill_lock:
            if page.wallpapers and self._like_page < LIKE_PAGES_PER_FAVOURITE:
                self._like_seed = page.seed or self._like_seed
                self._like_page += 1
                return
            if self._like_subject is not None:
                self._like_walked.add(self._like_subject)
            self._like_subject, self._like_seed, self._like_page = None, None, 1

    def _admit_to_pool(
        self, wallpapers: Sequence[Wallpaper], settings: Settings, *, source: RefillStrategy
    ) -> None:
        """Put the **Wallpapers** that pass every **Filter** into the **Pool**, checked locally whatever was
        asked.

        A **Wallpaper** already in the **Pool** keeps the `source` and `fetched_at` it arrived with. One the
        **Decision log** mentions is refused (ADR 0016), though its `wallpapers` row is still refreshed.
        """
        passing = [w for w in _distinct(wallpapers) if _passes_filters(w, settings)]
        if not passing:
            return
        fetched_at = self._clock.now().isoformat()
        with self._write() as write:
            write.executemany(_UPSERT_WALLPAPER, [_wallpaper_row(w) for w in passing])
            write.executemany(
                _ADMIT_TO_POOL,
                [{"id": w.id, "fetched_at": fetched_at, "source": source.value} for w in passing],
            )

    def _prune_pool(self, write: sqlite3.Connection, settings: Settings) -> None:
        """Drop every **Pool** member that no longer passes the **Filters**, after every settings write.

        Membership only: the `wallpapers` row and the **Decision log** stay, and the live **Batch** is
        untouched.
        """
        rows = write.execute(_SELECT_POOL_WALLPAPERS).fetchall()
        failing = [
            (str(row["id"]),) for row in rows if not _passes_filters(_wallpaper_from_row(row), settings)
        ]
        if failing:
            write.executemany("DELETE FROM pool WHERE wallpaper_id = ?", failing)

    def _pool_size(self) -> int:
        return int(self._connect().execute("SELECT COUNT(*) FROM pool").fetchone()[0])

    def _pool_wallpapers(self) -> list[Wallpaper]:
        """Every **Wallpaper** in the **Pool**, ordered so a seeded random source draws the same sample."""
        rows = self._connect().execute(_SELECT_POOL_WALLPAPERS).fetchall()
        return [_wallpaper_from_row(row) for row in rows]

    def _live_batch(self) -> Batch | None:
        """The unsubmitted **Batch**, if there is one."""
        return _load_live_batch(self._connect())

    def set_draft_verdict(
        self, batch_id: str, wallpaper_id: str, verdict: Verdict | None
    ) -> SubmissionRefused | None:
        """Mark one tile of the **Draft Batch**, or clear it with `None`.

        Sets rather than toggles, so a replayed click cannot flip the state; clearing deletes the row, because
        absence is an **Ignore**. Refused for an unknown or submitted **Batch**.
        """
        refusal: SubmissionRefused | None = None
        with self._write() as write:
            batch = write.execute("SELECT submitted_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                refusal = SubmissionRefused(reason=SubmissionRefused.Reason.UNKNOWN_BATCH)
            elif batch["submitted_at"] is not None:
                refusal = SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)
            elif verdict is None:
                write.execute(
                    "DELETE FROM draft_batch WHERE batch_id = ? AND wallpaper_id = ?",
                    (batch_id, wallpaper_id),
                )
            else:
                write.execute(
                    "INSERT INTO draft_batch (batch_id, wallpaper_id, verdict) VALUES (?, ?, ?)"
                    " ON CONFLICT (batch_id, wallpaper_id) DO UPDATE SET verdict = excluded.verdict",
                    (batch_id, wallpaper_id, verdict.value),
                )
        return refusal

    def set_all_draft_verdicts(self, batch_id: str, verdict: Verdict | None) -> SubmissionRefused | None:
        """Rewrite the whole **Draft Batch** in one transaction: select-all, or select-none with `None`.

        One transaction rather than one post per tile, so nothing can read the **Batch** half-marked. "All" is
        every tile shown, marked or not.
        """
        refusal: SubmissionRefused | None = None
        with self._write() as write:
            batch = write.execute("SELECT submitted_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                refusal = SubmissionRefused(reason=SubmissionRefused.Reason.UNKNOWN_BATCH)
            elif batch["submitted_at"] is not None:
                refusal = SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)
            else:
                # Scoped to this **Batch**. An unscoped delete reads the same on a database holding one
                # **Draft Batch** and is a silent data loss on any that holds two.
                write.execute("DELETE FROM draft_batch WHERE batch_id = ?", (batch_id,))
                if verdict is not None:
                    write.execute(_MARK_WHOLE_BATCH, (verdict.value, batch_id))
        return refusal

    def submit_batch(self, batch_id: str) -> Batch | BatchUnavailable | SubmissionRefused:
        """Append the **Batch**'s **Verdicts** to the **Decision log**, then hand back the next **Batch**.

        One transaction: claim the **Batch**, append the **Explicit Verdicts** and an **Ignore** for every
        unmarked tile, retire everything shown from the **Pool** (ADR 0016), clear the **Draft Batch**. `BEGIN
        IMMEDIATE` takes the write lock before the claim is read, so a second tab cannot split them.
        """
        recorded_at = self._clock.now().isoformat()
        with self._write() as write:
            batch = write.execute("SELECT submitted_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                return SubmissionRefused(reason=SubmissionRefused.Reason.UNKNOWN_BATCH)
            if batch["submitted_at"] is not None:
                return SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)

            shown = write.execute(
                "SELECT wallpaper_id FROM batch_wallpapers WHERE batch_id = ? ORDER BY position",
                (batch_id,),
            ).fetchall()
            drafted = _load_drafts(write, batch_id)
            write.executemany(
                "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, ?, ?, ?)",
                [
                    (
                        row["wallpaper_id"],
                        batch_id,
                        drafted.get(str(row["wallpaper_id"]), Verdict.IGNORE).value,
                        recorded_at,
                    )
                    for row in shown
                ],
            )
            # Decided once (ADR 0016): everything shown leaves the **Pool** in this transaction.
            write.executemany(
                "DELETE FROM pool WHERE wallpaper_id = ?", [(row["wallpaper_id"],) for row in shown]
            )
            write.execute("DELETE FROM draft_batch WHERE batch_id = ?", (batch_id,))
            write.execute("UPDATE batches SET submitted_at = ? WHERE id = ?", (recorded_at, batch_id))

        # Outside the transaction, deliberately: a download is a network call, and nothing the **Library**
        # does may roll the **Decision log** back. Idempotent, so a failure is picked up next time.
        self.reconcile_library()
        # Before `get_next_batch`, so nothing about to be drawn is being counted as evictable.
        self.evict_thumbnails()
        return self.get_next_batch()

    # -- library ---------------------------------------------------------------------------------------

    def reconcile_library(self) -> LibraryReconciliation:
        """Make the **Library** folder agree with the **Decision log**, and report what that took.

        Idempotent and derived: a **Favourite** with no recorded file gets one, a recorded file whose
        **Wallpaper** is no longer a **Favourite** is deleted. Failures are collected rather than raised,
        because this runs after the **Decision log** has committed. Nothing here stats a path.
        """
        library_path = self.get_settings().library_path
        rows = self._connect().execute(_LIBRARY_CANDIDATES, (Verdict.FAVOURITE.value,)).fetchall()
        resolved = self.resolve_verdicts([str(row["wallpaper_id"]) for row in rows])

        written: list[str] = []
        removed: list[str] = []
        failed: list[str] = []
        for row in rows:
            wallpaper_id = str(row["wallpaper_id"])
            recorded = None if row["path"] is None else Path(str(row["path"]))
            wanted = resolved[wallpaper_id].verdict is Verdict.FAVOURITE
            try:
                if wanted and recorded is None:
                    if self._add_to_library(wallpaper_id, str(row["full_url"]), library_path):
                        written.append(wallpaper_id)
                    else:
                        failed.append(wallpaper_id)
                elif not wanted and recorded is not None:
                    deleted = self._drop_from_library(wallpaper_id, recorded, library_path)
                    # A row outside the **Library** is dropped with its file left where it is.
                    if deleted:
                        removed.append(wallpaper_id)
            except Exception:
                # The writer declares no error type. Leaving the row as it was makes the next call retry.
                failed.append(wallpaper_id)
        return LibraryReconciliation(written=tuple(written), removed=tuple(removed), failed=tuple(failed))

    def download_favourites(self) -> FavouriteDownload:
        """Write a **Library** file for every **Favourite** that has not got one. Never deletes.

        The one place wallpapi asks the folder anything: whether a recorded path is still there. A
        **Favourite** counts as missing its file with no record, a recorded path not on the disk, or one no
        longer confined; it is written into the **Library path** of today and the record replaced.
        """
        library_path = self.get_settings().library_path
        rows = self._connect().execute(_LIBRARY_CANDIDATES, (Verdict.FAVOURITE.value,)).fetchall()
        resolved = self.resolve_verdicts([str(row["wallpaper_id"]) for row in rows])

        written: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for row in rows:
            wallpaper_id = str(row["wallpaper_id"])
            if resolved[wallpaper_id].verdict is not Verdict.FAVOURITE:
                continue
            recorded = None if row["path"] is None else Path(str(row["path"]))
            # The guard first, so the only paths ever stat-ed are inside the **Library** folder.
            held = None if recorded is None else confined_to_library(recorded, library_path)
            if held is not None and held.exists():
                skipped.append(wallpaper_id)
                continue
            try:
                if self._add_to_library(wallpaper_id, str(row["full_url"]), library_path):
                    written.append(wallpaper_id)
                else:
                    failed.append(wallpaper_id)
            except Exception:
                # As in `reconcile_library`: the writer declares no error type.
                failed.append(wallpaper_id)
        return FavouriteDownload(written=tuple(written), skipped=tuple(skipped), failed=tuple(failed))

    def _add_to_library(self, wallpaper_id: str, source_url: str, library_path: Path) -> bool:
        """Download one **Favourite** and record where it landed, or return `False` before fetching anything.

        The confinement check comes first, so a refusal never costs a download.
        """
        name = library_file_name(wallpaper_id, source_url)
        if name is None:
            return False
        destination = confined_to_library(library_path / name, library_path)
        if destination is None:
            return False
        written = self._library.write(wallpaper_id, source_url, destination)
        with self._write() as write:
            write.execute(_RECORD_LIBRARY_FILE, (wallpaper_id, str(written), self._clock.now().isoformat()))
        return True

    def _drop_from_library(self, wallpaper_id: str, recorded: Path, library_path: Path) -> bool:
        """Forget a recorded **Library** file, deleting it only if it is still confined (invariant 9).

        The row goes either way, so a refusal happens once rather than on every submission. Returns whether
        the file was handed to the writer; a failed deletion raises with the row intact, to be retried.
        """
        confined = confined_to_library(recorded, library_path)
        if confined is not None:
            self._library.remove(confined)
        with self._write() as write:
            write.execute("DELETE FROM library_files WHERE wallpaper_id = ?", (wallpaper_id,))
        return confined is not None

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
        destination = self.thumbnail_dir / f"{wallpaper_id}{_url_suffix(source_url)}"
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
            destination = self.thumbnail_dir / f"{wallpaper_id}{_url_suffix(source_url)}"
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

        decided = self._explicitly_decided()
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

    def _explicitly_decided(self) -> set[str]:
        """The **Wallpapers** whose resolved **Verdict** is an **Explicit Verdict**."""
        return {str(row["wallpaper_id"]) for row in self._connect().execute(_EXPLICITLY_DECIDED)}

    def _awaiting_a_verdict(self) -> set[str]:
        """The **Pool** and the live **Batch**, which can hold a **Wallpaper** pruned from the **Pool**
        since.
        """
        return {str(row["wallpaper_id"]) for row in self._connect().execute(_AWAITING_A_VERDICT)}

    # -- verdict resolution ----------------------------------------------------------------------------

    def resolve_verdicts(self, wallpaper_ids: Sequence[str]) -> dict[str, ResolvedVerdict]:
        """**Verdict resolution** for many **Wallpapers** at once, in one query.

        No entries, a legacy **Clearance** last, and an unknown id all resolve to absent and zero.
        """
        requested = list(dict.fromkeys(wallpaper_ids))
        resolved = dict.fromkeys(requested, _ABSENT)
        if not requested:
            return resolved
        # SQLite's parameter limit is 32,766 here, well above any **Pool** this is handed.
        placeholders = ",".join("?" * len(requested))
        query = _resolution_query(
            "SELECT wallpaper_id, resolved FROM resolution",
            restriction=f"WHERE wallpaper_id IN ({placeholders})",
        )
        for row in self._connect().execute(query, requested).fetchall():
            resolved[str(row["wallpaper_id"])] = _resolved_from(row["resolved"])
        return resolved

    # -- history ---------------------------------------------------------------------------------------

    def edit_verdict(self, wallpaper_id: str, verdict: Verdict) -> HistoryRefused | None:
        """Change a **Wallpaper**'s **Verdict** from **History** by appending an entry with `batch_id` `NULL`.

        An **Ignore** is how **History** withdraws a **Verdict**, un-**Banning** included. An unknown
        **Wallpaper** is refused rather than left to the foreign key. The **Library** is reconciled
        afterwards, outside the transaction.
        """
        recorded_at = self._clock.now().isoformat()
        with self._write() as write:
            if not _wallpaper_exists(write, wallpaper_id):
                return HistoryRefused(reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER)
            write.execute(_APPEND_HISTORY_ENTRY, (wallpaper_id, verdict.value, recorded_at))
        self.reconcile_library()
        return None

    def list_history_rows(self, *, verdict: Verdict | None = None, page: int = 1) -> HistoryPage:
        """One page of **History**, newest activity first, filtered by resolved **Verdict** in SQL.

        `page` is clamped rather than refused, so a stale link shows the last page.
        """
        parameters: list[str] = [] if verdict is None else [verdict.value]
        filtered = verdict is not None
        connection = self._connect()
        total = int(connection.execute(_history_count_query(filtered=filtered), parameters).fetchone()[0])
        pages = max(1, -(-total // HISTORY_PAGE_SIZE))
        wanted = min(max(page, 1), pages)
        rows = connection.execute(
            _history_rows_query(filtered=filtered),
            [*parameters, HISTORY_PAGE_SIZE, (wanted - 1) * HISTORY_PAGE_SIZE],
        ).fetchall()
        return HistoryPage(
            rows=self._history_rows_from(rows), page=wanted, pages=pages, total=total, verdict=verdict
        )

    def get_history_row(self, wallpaper_id: str) -> HistoryRow | None:
        """One **Wallpaper**'s **History** row, or `None`: the same query as the listing, for an edit to swap
        in.
        """
        row = self._connect().execute(_HISTORY_ROW, (wallpaper_id,)).fetchone()
        if row is None:
            return None
        return self._history_rows_from([row])[0]

    def _history_rows_from(self, rows: Sequence[sqlite3.Row]) -> tuple[HistoryRow, ...]:
        """Turn listing rows into **History** rows, resolving the page's **Wallpapers** in one call."""
        resolved = self.resolve_verdicts([str(row["id"]) for row in rows])
        return tuple(
            HistoryRow(
                wallpaper=_wallpaper_from_row(row),
                resolved=resolved[str(row["id"])],
                latest_at=dt.datetime.fromisoformat(str(row["latest_at"])),
            )
            for row in rows
        )

    def list_history(
        self, *, batch_id: str | None = None, wallpaper_id: str | None = None
    ) -> list[DecisionEntry]:
        """The **Decision log**'s raw entries in sequence order, optionally for one **Batch** or
        **Wallpaper**.
        """
        conditions: list[str] = []
        parameters: list[str] = []
        if batch_id is not None:
            conditions.append("batch_id = ?")
            parameters.append(batch_id)
        if wallpaper_id is not None:
            conditions.append("wallpaper_id = ?")
            parameters.append(wallpaper_id)
        query = "SELECT seq, wallpaper_id, batch_id, verdict, recorded_at FROM decision_log"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        rows = self._connect().execute(f"{query} ORDER BY seq", parameters).fetchall()
        return [
            DecisionEntry(
                seq=int(row["seq"]),
                wallpaper_id=str(row["wallpaper_id"]),
                batch_id=None if row["batch_id"] is None else str(row["batch_id"]),
                entry=_entry_from(row["verdict"]),
                recorded_at=dt.datetime.fromisoformat(str(row["recorded_at"])),
            )
            for row in rows
        ]


def _alternated(last: RefillStrategy | None, *, has_favourites: bool) -> RefillStrategy:
    """Whichever strategy did not take the last step, while both have work: strict turns are an even split."""
    if not has_favourites or last is RefillStrategy.LIKE:
        return RefillStrategy.RANDOM
    return RefillStrategy.RANDOM if last is None else RefillStrategy.LIKE


def _validated_batch_size(value: int | str) -> int | SettingsRefused.Reason:
    """The batch size, or the reason it is not one. "1.5" is refused, not rounded."""
    try:
        size = int(str(value).strip())
    except ValueError:
        return SettingsRefused.Reason.BATCH_SIZE_NOT_A_NUMBER
    if not MIN_BATCH_SIZE <= size <= MAX_BATCH_SIZE:
        return SettingsRefused.Reason.BATCH_SIZE_OUT_OF_RANGE
    return size


def _passes_filters(wallpaper: Wallpaper, settings: Settings) -> bool:
    """Every **Filter**, checked locally: the one rule for admitting to the **Pool** and for pruning it.

    Purity, `atleast` and `ratios` are checked here as well as sent: the API is trusted but not relied upon.
    """
    return (
        wallpaper.purity.strip().lower() == SFW_PURITY_NAME
        and wallpaper.width >= settings.min_width
        and wallpaper.height >= settings.min_height
        and wallpaper.favourites >= settings.min_favourites
        and _matches_an_allowed_ratio(wallpaper, settings.allowed_ratios)
    )


def _matches_an_allowed_ratio(wallpaper: Wallpaper, allowed: Sequence[str]) -> bool:
    """Whether the **Wallpaper**'s own shape is within `RATIO_TOLERANCE` of an allowed ratio.

    Computed from width and height, not Wallhaven's `ratio`, which is rounded; a band because Wallhaven's
    `ratios=` buckets: a 3440x1440 is 2.39 and served under `21x9`.
    """
    if wallpaper.height <= 0:
        return False
    shape = wallpaper.width / wallpaper.height
    return any(
        abs(shape - named) <= RATIO_TOLERANCE
        for named in (_ratio_value(ratio) for ratio in allowed)
        if named is not None
    )


def _ratio_value(named: str) -> float | None:
    """`"16x9"` as 1.777…, or `None`; it is also reached with whatever a hand-edited row holds."""
    width, _, height = named.partition("x")
    try:
        return int(width) / int(height)
    except ValueError, ZeroDivisionError:
        return None


def _whole_number(value: object) -> int | None:
    """The value as a whole number, or `None`: "1.5" and "1e3" are refused rather than coerced."""
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _validated_pool_target_size(value: int | str) -> int | SettingsRefused.Reason:
    """The **Pool** target size, or the reason it is not one."""
    size = _whole_number(value)
    if size is None or not MIN_POOL_TARGET_SIZE <= size <= MAX_POOL_TARGET_SIZE:
        return SettingsRefused.Reason.POOL_TARGET_SIZE_INVALID
    return size


def _validated_min_width(value: int | str) -> int | SettingsRefused.Reason:
    return _validated_pixels(value, SettingsRefused.Reason.MIN_WIDTH_INVALID)


def _validated_min_height(value: int | str) -> int | SettingsRefused.Reason:
    return _validated_pixels(value, SettingsRefused.Reason.MIN_HEIGHT_INVALID)


def _validated_pixels(value: int | str, reason: SettingsRefused.Reason) -> int | SettingsRefused.Reason:
    """One axis of the minimum resolution, or the reason it is not one. Zero means no minimum."""
    pixels = _whole_number(value)
    if pixels is None or not 0 <= pixels <= MAX_FILTER_PIXELS:
        return reason
    return pixels


def _validated_min_favourites(value: int | str) -> int | SettingsRefused.Reason:
    """The minimum **Favourites**, or the reason it is not one. No ceiling: no value is clearly a typo."""
    favourites = _whole_number(value)
    if favourites is None or favourites < 0:
        return SettingsRefused.Reason.MIN_FAVOURITES_INVALID
    return favourites


def _decimal_number(value: object) -> float | None:
    """The value as a finite decimal, or `None`. NaN parses, and a NaN radius would make everything
    **Unknown**.
    """
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _validated_similarity_radius(value: float | str) -> float | SettingsRefused.Reason:
    """The similarity radius in `[0, 1]`, or the reason it is not one: above 1 it would do nothing."""
    number = _decimal_number(value)
    if number is None or not 0.0 <= number <= 1.0:
        return SettingsRefused.Reason.SIMILARITY_RADIUS_INVALID
    return number


def _validated_similarity_decay(value: float | str) -> float | SettingsRefused.Reason:
    """The similarity decay, or the reason it is not one. Negative would invert the rule."""
    number = _decimal_number(value)
    if number is None or not 0.0 <= number <= MAX_SIMILARITY_DECAY:
        return SettingsRefused.Reason.SIMILARITY_DECAY_INVALID
    return number


def _validated_thumbnail_cache_max_mb(value: int | str) -> int | SettingsRefused.Reason:
    """The **Thumbnail cache** cap in megabytes, or the reason it is not one.

    Zero is accepted: the cap never evicts an **Explicit Verdict**, so **History** still renders.
    """
    megabytes = _whole_number(value)
    if megabytes is None or megabytes < 0:
        return SettingsRefused.Reason.THUMBNAIL_CACHE_MAX_MB_INVALID
    return megabytes


def _validated_allowed_ratios(value: Sequence[str] | str) -> tuple[str, ...] | SettingsRefused.Reason:
    """The allowed ratios, trimmed and de-duplicated in order, or the reason they are not.

    Checked against `WALLHAVEN_RATIOS`; an empty list is refused as more likely a mistake than an intention.
    """
    parts = value.split(",") if isinstance(value, str) else list(value)
    named = list(dict.fromkeys(part.strip() for part in parts))
    if not named or any(part not in WALLHAVEN_RATIOS for part in named):
        return SettingsRefused.Reason.ALLOWED_RATIOS_INVALID
    return tuple(named)


def _validated_library_path(value: Path | str) -> Path | SettingsRefused.Reason:
    """The **Library** path, absolute, or the reason it is not one. Never touches the filesystem."""
    text = str(value).strip()
    if not text:
        return SettingsRefused.Reason.LIBRARY_PATH_EMPTY
    path = Path(text)
    if not path.is_absolute():
        return SettingsRefused.Reason.LIBRARY_PATH_NOT_ABSOLUTE
    return path


LIBRARY_FILE_NAME = re.compile(r"[A-Za-z0-9]+\.[a-z0-9]{1,5}")
"""The only shape a **Library** file name may take: a Wallhaven ID, a dot, one short extension.

A name is never a path: `..`, separators, spaces and a second dot are all refused. ASCII by construction,
because `str.isalnum` counts `²`.
"""

DEFAULT_LIBRARY_SUFFIX = ".jpg"
"""What a `full_url` with no recognisable extension is saved as: Wallhaven serves JPEG nearly always."""


def library_file_name(wallpaper_id: str, source_url: str) -> str | None:
    """What a **Library** file is called, or `None` if this **Wallpaper** cannot have one.

    A bad suffix falls back to `.jpg`; a bad ID names nothing. The suffix is Wallhaven's to get wrong, and the
    ID is what the file is for.
    """
    named = f"{wallpaper_id}{_url_suffix(source_url, default=DEFAULT_LIBRARY_SUFFIX)}"
    if LIBRARY_FILE_NAME.fullmatch(named):
        return named
    fallback = f"{wallpaper_id}{DEFAULT_LIBRARY_SUFFIX}"
    return fallback if LIBRARY_FILE_NAME.fullmatch(fallback) else None


def confined_to_library(path: Path, library_root: Path) -> Path | None:
    """The resolved `path`, if wallpapi may write or delete it — otherwise `None`. The one guard for both.

    The name fits `LIBRARY_FILE_NAME`, and the path, fully resolved (`..` collapsed, every symlink and
    junction followed), lies strictly inside the fully resolved **Library** folder. Resolving is the point: a
    link in the folder is inside it syntactically and outside it in fact. Compared through `os.path.normcase`
    and by component, because NTFS ignores case and a string prefix would put `Library2` inside `Library`.

    The resolved path is returned because it is the one the caller must use: the atomic write's temp file is a
    sibling of whatever it is handed. `strict=False` because the folder need not exist yet.
    """
    try:
        resolved = path.resolve(strict=False)
        root = library_root.resolve(strict=False)
    except OSError, ValueError:
        # A hand-edited row can hold characters Windows will not even parse. Unresolvable is refused.
        return None
    if not LIBRARY_FILE_NAME.fullmatch(resolved.name):
        return None
    here = os.path.normcase(str(resolved))
    there = os.path.normcase(str(root))
    if here == there:
        return None
    try:
        if os.path.commonpath((here, there)) != there:
            return None
    except ValueError:
        # Different drives, or one of the two not absolute. Either way there is no "inside" to be in.
        return None
    return resolved


def validated_mix(
    name: str, *, unknown: int | str, banger: int | str, dud: int | str
) -> Mix | SettingsRefused.Reason:
    """A **Mix**, or the reason those numbers are not one. The one place that decides what a **Mix** is.

    Whole percentages, none negative, summing to exactly `MIX_TOTAL`; a name trimmed, non-empty and at most
    `MAX_MIX_NAME_LENGTH`, compared case-sensitively.
    """
    trimmed = name.strip()
    if not trimmed or len(trimmed) > MAX_MIX_NAME_LENGTH:
        return SettingsRefused.Reason.MIX_NAME_INVALID
    percentages = [_whole_number(value) for value in (unknown, banger, dud)]
    if any(share is None or share < 0 for share in percentages):
        return SettingsRefused.Reason.MIX_PERCENTAGES_INVALID
    shares = [share for share in percentages if share is not None]
    if sum(shares) != MIX_TOTAL:
        return SettingsRefused.Reason.MIX_PERCENTAGES_INVALID
    return Mix(name=trimmed, unknown=shares[0], banger=shares[1], dud=shares[2])


_UPSERT_SETTING = """
INSERT INTO settings (key, value) VALUES (?, ?)
ON CONFLICT (key) DO UPDATE SET value = excluded.value
"""

_UPSERT_MIX = """
INSERT INTO mixes (name, unknown, banger, dud) VALUES (?, ?, ?, ?)
ON CONFLICT (name) DO UPDATE
SET unknown = excluded.unknown, banger = excluded.banger, dud = excluded.dud
"""
"""One statement for creating and editing a **Mix**: the name is the key, so there is no rename."""

_DELETE_MIX = "DELETE FROM mixes WHERE name = ?"

_LIBRARY_CANDIDATES = """
SELECT w.id AS wallpaper_id, w.full_url AS full_url, f.path AS path
FROM wallpapers AS w
LEFT JOIN library_files AS f ON f.wallpaper_id = w.id
WHERE f.wallpaper_id IS NOT NULL
   OR w.id IN (SELECT wallpaper_id FROM decision_log WHERE verdict = ?)
ORDER BY w.id
"""
"""Everything a reconciliation might act on: ever **Favourited**, or holding a recorded file.

A superset: `resolve_verdicts` decides which still count, so the rule is not spelled a second time here.
"""

_FAVOURITED_AT_LEAST_ONCE = """
SELECT DISTINCT wallpaper_id FROM decision_log WHERE verdict = ? ORDER BY wallpaper_id
"""
"""Every **Wallpaper** that could resolve to **Favourite**; a superset for `resolve_verdicts` to narrow."""

_RECORD_LIBRARY_FILE = """
INSERT INTO library_files (wallpaper_id, path, written_at) VALUES (?, ?, ?)
ON CONFLICT (wallpaper_id) DO UPDATE SET path = excluded.path, written_at = excluded.written_at
"""
"""An upsert, so an unexpected existing row is overwritten rather than an `IntegrityError` retried for
ever.
"""

_ABSENT = ResolvedVerdict(verdict=None, value=0)
"""A **Wallpaper** with no **Decision log** entries at all."""

_EXPLICIT_VALUES = {Verdict.FAVOURITE: 100, Verdict.LIKE: 50, Verdict.BAN: -100}
"""What each **Explicit Verdict** resolves to. An **Ignore** is not here — it is `_IGNORE_VALUE`."""

_IGNORE_VALUE = -10
"""What a resolved **Ignore** is worth. Once, never stacked."""


def _entry_from(stored: object) -> Verdict | Clearance:
    """One `decision_log.verdict` cell as the entry it is: the column also holds legacy **Clearances**."""
    text = str(stored)
    return Clearance.CLEARED if text == Clearance.CLEARED.value else Verdict(text)


def _is_explicit(resolved: ResolvedVerdict) -> bool:
    """Whether what stands is an **Explicit Verdict**: present and not an **Ignore**. Asked by pre-marking."""
    return resolved.verdict is not None and resolved.verdict is not Verdict.IGNORE


def _resolved_from(resolved: object) -> ResolvedVerdict:
    """What a resolved **Verdict** is worth. The rule that *chose* it is `_RESOLUTION_CTE`, never copied
    here.
    """
    if resolved is None:
        return _ABSENT
    verdict = Verdict(str(resolved))
    if verdict is Verdict.IGNORE:
        return ResolvedVerdict(verdict=verdict, value=_IGNORE_VALUE)
    return ResolvedVerdict(verdict=verdict, value=_EXPLICIT_VALUES[verdict])


_RESOLUTION_CTE = f"""
WITH entries AS (
    SELECT wallpaper_id, MAX(seq) AS latest_seq
    FROM decision_log
    {{restriction}}
    GROUP BY wallpaper_id
),
resolution AS (
    SELECT
        entries.wallpaper_id,
        entries.latest_seq,
        CASE WHEN latest.verdict != '{Clearance.CLEARED.value}' THEN latest.verdict END AS resolved
    FROM entries
    JOIN decision_log AS latest ON latest.seq = entries.latest_seq
)
"""
"""**Verdict resolution**, as the prelude to every query that needs it (ADR 0015).

The latest entry decides, by `MAX(seq)` and never `recorded_at`: one submission's entries share a timestamp. A
legacy **Clearance** as the latest entry resolves to `NULL`. In SQL because **History** filters and pages over
the resolved **Verdict**.

`{{restriction}}` is a `WHERE` built here, never caller text; values are bound as parameters.
"""


def _resolution_query(select: str, *, restriction: str = "") -> str:
    """A query over the resolved **Decision log**: the shared CTE, then whatever the caller selects."""
    return _RESOLUTION_CTE.format(restriction=restriction) + select


_HISTORY_SELECT = """
SELECT w.*, resolution.resolved AS resolved, latest.recorded_at AS latest_at
FROM resolution
JOIN wallpapers AS w ON w.id = resolution.wallpaper_id
JOIN decision_log AS latest ON latest.seq = resolution.latest_seq
"""
"""One **History** row per **Wallpaper**: `latest` joined on `latest_seq`, so its timestamp is the deciding
one.
"""


def _history_rows_query(*, filtered: bool) -> str:
    """The **History** listing, by `latest_seq` and never `latest_at`, which a whole **Batch** shares."""
    where = "WHERE resolution.resolved = ?" if filtered else ""
    return _resolution_query(
        f"{_HISTORY_SELECT}{where}\nORDER BY resolution.latest_seq DESC\nLIMIT ? OFFSET ?"
    )


def _history_count_query(*, filtered: bool) -> str:
    """How many rows the same filter matches, for the paging."""
    where = "WHERE resolved = ?" if filtered else ""
    return _resolution_query(f"SELECT COUNT(*) FROM resolution {where}")


_HISTORY_ROW = _resolution_query(_HISTORY_SELECT, restriction="WHERE wallpaper_id = ?")
"""One **Wallpaper**'s **History** row."""

_EXPLICITLY_DECIDED = _resolution_query(
    f"SELECT wallpaper_id FROM resolution WHERE resolved IS NOT NULL AND resolved != '{Verdict.IGNORE.value}'"
)
"""Every **Wallpaper** with an **Explicit Verdict** standing: never evicted from the **Thumbnail cache**."""

_AWAITING_A_VERDICT = """
SELECT wallpaper_id FROM pool
UNION
SELECT bw.wallpaper_id
FROM batch_wallpapers AS bw
JOIN batches AS b ON b.id = bw.batch_id
WHERE b.submitted_at IS NULL
"""
"""Every **Wallpaper** still to be shown: the **Pool**, plus the live **Batch**."""

_APPEND_HISTORY_ENTRY = """
INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, NULL, ?, ?)
"""
"""An edit made from **History**. `batch_id` is `NULL`, which is what keeps it out of a **Batch**'s
entries.
"""


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


def _load_live_batch(connection: sqlite3.Connection) -> Batch | None:
    """The most recent unsubmitted **Batch**, rebuilt from storage, or `None`."""
    batch = connection.execute(
        "SELECT id, created_at, size FROM batches WHERE submitted_at IS NULL ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if batch is None:
        return None
    rows = connection.execute(_SELECT_BATCH_WALLPAPERS, (batch["id"],)).fetchall()
    return Batch(
        id=str(batch["id"]),
        size=int(batch["size"]),
        created_at=dt.datetime.fromisoformat(str(batch["created_at"])),
        wallpapers=tuple(_wallpaper_from_row(row) for row in rows),
        drafts=_load_drafts(connection, str(batch["id"])),
        # A **Batch** minted before migration 6 has NULL here, and its tiles go unlabelled.
        zones={str(row["id"]): Zone(str(row["zone"])) for row in rows if row["zone"] is not None},
    )


def _load_drafts(connection: sqlite3.Connection, batch_id: str) -> dict[str, Verdict]:
    """The **Draft Batch**: only the **Wallpapers** actually marked, because absence means **Ignore**."""
    rows = connection.execute(
        "SELECT wallpaper_id, verdict FROM draft_batch WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    return {str(row["wallpaper_id"]): Verdict(str(row["verdict"])) for row in rows}


def _wallpaper_from_row(row: sqlite3.Row) -> Wallpaper:
    return Wallpaper(
        id=str(row["id"]),
        width=int(row["width"]),
        height=int(row["height"]),
        ratio=str(row["ratio"]),
        category=str(row["category"]),
        purity=str(row["purity"]),
        favourites=int(row["favourites"]),
        colours=tuple(str(row["colours"]).split(",")) if row["colours"] else (),
        thumbnail_url=str(row["thumbnail_url"]),
        full_url=str(row["full_url"]),
        page_url=str(row["page_url"]),
    )


def _url_suffix(url: str, *, default: str = ".jpg") -> str:
    """The file extension of a URL's path, ignoring any query string."""
    return PurePosixPath(urlsplit(url).path).suffix or default


def _distinct(wallpapers: Sequence[Wallpaper]) -> list[Wallpaper]:
    """Distinct **Wallpapers** by ID, first occurrence winning: a random search can repeat one."""
    seen: set[str] = set()
    unique: list[Wallpaper] = []
    for wallpaper in wallpapers:
        if wallpaper.id not in seen:
            seen.add(wallpaper.id)
            unique.append(wallpaper)
    return unique


def _wallpaper_row(wallpaper: Wallpaper) -> tuple[str | int, ...]:
    return (
        wallpaper.id,
        wallpaper.width,
        wallpaper.height,
        wallpaper.ratio,
        wallpaper.category,
        wallpaper.purity,
        wallpaper.favourites,
        ",".join(wallpaper.colours),
        wallpaper.thumbnail_url,
        wallpaper.full_url,
        wallpaper.page_url,
    )


_ADMIT_TO_POOL = """
INSERT INTO pool (wallpaper_id, fetched_at, source)
SELECT :id, :fetched_at, :source
WHERE NOT EXISTS (SELECT 1 FROM decision_log WHERE wallpaper_id = :id)
ON CONFLICT (wallpaper_id) DO NOTHING
"""
"""Admit unless the **Decision log** mentions it at all, a legacy `cleared` entry included (ADR 0016).

`DO NOTHING`, so `fetched_at` stays the first arrival.
"""

_SELECT_POOL_WALLPAPERS = """
SELECT w.*
FROM pool
JOIN wallpapers AS w ON w.id = pool.wallpaper_id
ORDER BY pool.rowid
"""
"""The whole **Pool**, in a fixed order, so a seeded draw is reproducible."""

_SELECT_POOL_THUMBNAILS = """
SELECT w.id, w.thumbnail_url
FROM pool
JOIN wallpapers AS w ON w.id = pool.wallpaper_id
ORDER BY pool.wallpaper_id
"""
"""Every **Pool** member's thumbnail URL, in the order the downloader fetches them."""

_SELECT_DECIDED_WALLPAPERS = """
SELECT w.*
FROM wallpapers AS w
WHERE w.id IN (SELECT wallpaper_id FROM decision_log)
ORDER BY w.id
"""
"""Every **Wallpaper** the **Decision log** mentions, as whole rows, in a fixed order.

Whole rows because the **Similarity provider** is handed **Wallpapers**; ordered so the matrix's columns, and
so every **Score**, are reproducible.
"""

_SELECT_BATCH_WALLPAPERS = """
SELECT w.*, bw.zone AS zone
FROM batch_wallpapers AS bw
JOIN wallpapers AS w ON w.id = bw.wallpaper_id
WHERE bw.batch_id = ?
ORDER BY bw.position
"""

_MARK_WHOLE_BATCH = """
INSERT INTO draft_batch (batch_id, wallpaper_id, verdict)
SELECT batch_id, wallpaper_id, ?
FROM batch_wallpapers
WHERE batch_id = ?
"""
"""Select-all as one statement, read inside the same transaction as the delete."""

_UPSERT_WALLPAPER = """
INSERT INTO wallpapers
    (id, width, height, ratio, category, purity, favourites, colours, thumbnail_url, full_url, page_url)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
    width = excluded.width,
    height = excluded.height,
    ratio = excluded.ratio,
    category = excluded.category,
    purity = excluded.purity,
    favourites = excluded.favourites,
    colours = excluded.colours,
    thumbnail_url = excluded.thumbnail_url,
    full_url = excluded.full_url,
    page_url = excluded.page_url
"""

_MIGRATION_1 = (
    """
CREATE TABLE wallpapers (
    id            TEXT PRIMARY KEY,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    ratio         TEXT NOT NULL,
    category      TEXT NOT NULL,
    purity        TEXT NOT NULL,
    favourites    INTEGER NOT NULL,
    colours       TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL,
    full_url      TEXT NOT NULL,
    page_url      TEXT NOT NULL
)
""",
    """
CREATE TABLE batches (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    size         INTEGER NOT NULL,
    submitted_at TEXT
)
""",
    """
CREATE TABLE batch_wallpapers (
    batch_id     TEXT NOT NULL REFERENCES batches (id),
    wallpaper_id TEXT NOT NULL REFERENCES wallpapers (id),
    position     INTEGER NOT NULL,
    PRIMARY KEY (batch_id, wallpaper_id)
)
""",
    """
CREATE TABLE decision_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallpaper_id TEXT NOT NULL REFERENCES wallpapers (id),
    batch_id     TEXT REFERENCES batches (id),
    verdict      TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
)
""",
    """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""",
    "INSERT INTO settings (key, value) VALUES ('batch_size', '8')",
)
"""Migration 1, one statement per entry.

Not an `executescript`: that commits the open transaction first, taking the migration out of its `BEGIN
IMMEDIATE`.
"""


_MIGRATION_2 = (
    """
CREATE TABLE draft_batch (
    batch_id     TEXT NOT NULL REFERENCES batches (id),
    wallpaper_id TEXT NOT NULL REFERENCES wallpapers (id),
    verdict      TEXT NOT NULL,
    PRIMARY KEY (batch_id, wallpaper_id)
)
""",
)
"""Migration 2, the **Draft Batch**, keyed one **Verdict** per **Wallpaper** per **Batch**."""


_MIGRATION_4 = (
    """
CREATE TABLE library_files (
    wallpaper_id TEXT PRIMARY KEY REFERENCES wallpapers (id),
    path         TEXT NOT NULL,
    written_at   TEXT NOT NULL
)
""",
)
"""Migration 4: the absolute path of every **Library** file written, as the writer returned it.

Recorded rather than derived from the **Library** setting, which can change while the file does not move.
"""


def _migration_3() -> tuple[tuple[str, tuple[str, str]], ...]:
    """Migration 3: seed the settings. `DO NOTHING`, so a value already chosen is kept."""
    return tuple((_SEED_SETTING, (key, value)) for key, value in _defaults().items())


_SEED_SETTING = "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING"


_CREATE_POOL = """
CREATE TABLE pool (
    wallpaper_id TEXT PRIMARY KEY REFERENCES wallpapers (id),
    fetched_at   TEXT NOT NULL,
    source       TEXT NOT NULL
)
"""
"""**Pool** membership as its own table: a **Wallpaper** leaves the **Pool** and keeps its `wallpapers`
row.
"""


def _migration_5() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 5: the **Pool** table, and the **Filters** and **Pool** target size as seeded rows."""
    return ((_CREATE_POOL, ()), *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()))


def _migration_6() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 6: the **Zone** a **Batch** drew each **Wallpaper** from, and the similarity settings.

    `zone` is a fact about the **Batch**, not a stored **Score**. NULL for a **Batch** minted before it.
    """
    return (
        ("ALTER TABLE batch_wallpapers ADD COLUMN zone TEXT", ()),
        *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()),
    )


_CREATE_MIXES = """
CREATE TABLE mixes (
    name    TEXT PRIMARY KEY,
    unknown INTEGER NOT NULL,
    banger  INTEGER NOT NULL,
    dud     INTEGER NOT NULL
)
"""
"""**Mixes** as rows, one column per **Zone**: the three numbers mean nothing apart.

No CHECK on the sum: `validated_mix` holds the rule, and a bad row is dropped on the way out.
"""

_SEED_MIX = """
INSERT INTO mixes (name, unknown, banger, dud) VALUES (?, ?, ?, ?)
ON CONFLICT (name) DO NOTHING
"""
"""`DO NOTHING`, so an edited **Explore** survives a re-run. A migration must not undo a setting."""


def _migration_7() -> tuple[tuple[str, tuple[str | int, ...]], ...]:
    """Migration 7: **Mixes** as a table, seeded with **Explore** and **Refine**, and the active one as a
    setting.
    """
    return (
        (_CREATE_MIXES, ()),
        *((_SEED_MIX, (mix.name, mix.unknown, mix.banger, mix.dud)) for mix in DEFAULT_MIXES),
        *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()),
    )


def _migration_8() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 8: seeded the **Revisit weight**. That setting is gone, so on a fresh database this seeds
    nothing new, and migration 10 deletes the row.
    """
    return tuple((_SEED_SETTING, (key, value)) for key, value in _defaults().items())


_RETUNE_SETTING = "UPDATE settings SET value = ? WHERE key = ? AND value = ?"
"""Change a setting only where it still holds the value a previous migration seeded, so a choice survives."""


def _migration_9() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 9: the **Similarity radius** default moves from 0.5 to 0.15 (ADR 0013), only where
    untouched.
    """
    radius = _defaults()[_SIMILARITY_RADIUS]
    return (
        (_RETUNE_SETTING, (radius, _SIMILARITY_RADIUS, str(SUPERSEDED_SIMILARITY_RADIUS))),
        *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()),
    )


_REVISIT_WEIGHT_KEY = "revisit_weight"
"""The `settings` key migration 8 seeded, kept only so migration 10 can delete the row (ADR 0016)."""


def _migration_10() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 10: decide once (ADR 0016).

    The **Pool** gives up everything the **Decision log** mentions; the log is indexed by **Wallpaper** for
    admission; the **Revisit weight** row goes; the **Pool** target moves from 2000 to 500 where untouched. A
    **Pool** above its new target drains rather than being trimmed.
    """
    target = _defaults()[_POOL_TARGET_SIZE]
    return (
        ("DELETE FROM pool WHERE wallpaper_id IN (SELECT wallpaper_id FROM decision_log)", ()),
        ("CREATE INDEX IF NOT EXISTS decision_log_by_wallpaper ON decision_log (wallpaper_id)", ()),
        ("DELETE FROM settings WHERE key = ?", (_REVISIT_WEIGHT_KEY,)),
        (_RETUNE_SETTING, (target, _POOL_TARGET_SIZE, str(SUPERSEDED_POOL_TARGET_SIZE))),
        *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()),
    )
