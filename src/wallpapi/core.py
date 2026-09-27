"""The Core service: the single seam between the UI and everything else.

Invariant 1 — the UI and every test talk only to this class, and its five dependencies are injected.
Storage lives here too: SQLite is an in-process detail of the Core service, not another seam.
"""

from __future__ import annotations

import datetime as dt
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

from wallpapi.clock import Clock
from wallpapi.files import write_atomically
from wallpapi.library import LibraryWriter
from wallpapi.model import DecisionEntry, Verdict, Wallpaper
from wallpapi.ratelimit import CALLS_PER_MINUTE, wait_needed
from wallpapi.rng import SeededRandom
from wallpapi.similarity import SimilarityProvider
from wallpapi.wallhaven import RateLimited, SearchPage, Wallhaven

SCHEMA_VERSION = 5

SFW_PURITY = "100"
"""Wallhaven's purity mask, most significant bit first: SFW on, sketchy and NSFW off.

Fixed, never a setting. NSFW is what needs an API key, and wallpapi asks for none.
"""

SFW_PURITY_NAME = "sfw"
"""What a search *result* calls the same thing. The mask is a query parameter; this is a response field."""

ALL_CATEGORIES = "111"
"""Wallhaven's category mask: general, anime and people, all on. Fixed, never a setting — the **Filters**
are about the shape and the quality of a **Wallpaper**, not about its subject."""

IDLE_RECHECK_SECONDS = 30.0
"""How long the refill waits before looking again once the **Pool** is at target.

Not zero, because a spin would burn a core doing `SELECT COUNT(*)`, and not minutes, because the **Pool**
drops below target the moment a **Batch** is submitted and the backlog should be topped up before the next
one is asked for.
"""

ERROR_BACKOFF_SECONDS = 60.0
"""How long the refill waits after a failed **API call** that named no delay of its own.

A minute, which is the window the 45-per-minute budget is counted over: if Wallhaven is refusing calls, the
cheapest correct thing to do is to stop spending the budget for one whole window.
"""

POOL_SOURCE_RANDOM = "random"
"""How a **Pool** member got there. #13 adds `'like'` when the refill starts searching for lookalikes."""

MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 64
"""The accepted range for the batch size, inclusive at both ends.

One because a **Batch** of none is a page with nothing to decide on and no way back off it. Sixty-four
because the spec asks for "a big blitz of 16 or 32" and nothing larger.

The upper bound used to be justified by what one page load could fetch — 24 results a page and four capped
**API calls**. That reasoning went with the walk at #6: a **Batch** is now sampled from a **Pool** of
`pool_target_size`, which is 2000 by default, so what a **Batch** can be filled from is no longer the
binding constraint. Sixty-four stays because it is as many **Wallpapers** as anyone can judge at once.
"""

DEFAULT_BATCH_SIZE = 8

MIN_POOL_TARGET_SIZE = 1
MAX_POOL_TARGET_SIZE = 20_000
"""The accepted range for the **Pool** target size.

One, because a **Pool** of none is a **Batch** page with nothing on it and no way off, exactly as a
**Batch** size of none would be. Twenty thousand, because invariant 2 sizes the **Similarity provider**'s
matrix off the **Pool**, and every whole-**Pool** scan — **Scoring** at #9, the **Filter** prune here — is
linear in it. Without a ceiling the refill grows the **Pool** for ever and the system gets slower every day
without ever saying why.
"""

DEFAULT_POOL_TARGET_SIZE = 2000
"""Large on purpose: the refill spends the full 45-calls-per-minute budget until the **Pool** reaches this,
so a big target is what "always a backlog to process" looks like without unbounded growth. See ADR 0005."""

MAX_FILTER_PIXELS = 30_000
"""The largest minimum resolution worth accepting, per axis.

Comfortably above anything Wallhaven serves. A minimum above it can only be a typo, and its only effect
would be an empty **Pool** with nothing anywhere to explain why.
"""

DEFAULT_MIN_WIDTH = 2560
DEFAULT_MIN_HEIGHT = 1440
"""1440p, as a minimum rather than an exact size (`atleast`, never `resolutions`).

The spec's monitor is 1440p or 4K. This admits a 1440p wallpaper at its native size and everything larger,
and excludes the 1080p uploads that would have to be upscaled to fill either.
"""

DEFAULT_ALLOWED_RATIOS = ("16x9", "16x10", "21x9")
"""The shapes a desktop monitor actually is: ordinary widescreen, the 16:10 panels, and ultrawide."""

DEFAULT_MIN_FAVOURITES = 10
"""Enough to skip the long tail nobody has ever looked at, low enough not to collapse the **Pool** to a few
hundred famous images. Applied locally — Wallhaven's search has no parameter for it."""

WALLHAVEN_RATIOS = frozenset(
    {"16x9", "16x10", "21x9", "32x9", "48x9", "9x16", "10x16", "9x18", "1x1", "3x2", "4x3", "5x4"}
)
"""The `ratios=` values Wallhaven accepts, as its own search offers them.

Validated against rather than merely checked for being a string, because an unrecognised ratio is not an
error Wallhaven reports — it is a search that quietly returns something other than what was asked for.
"""

RATIO_TOLERANCE = 0.08
"""How far a **Wallpaper**'s own width/height may sit from a named ratio and still count as it.

Wallhaven's `ratios=` filter buckets rather than matching exactly: a 3440x1440 ultrawide is 2.39 and
Wallhaven serves it under `21x9`, which is 2.33. The local check is a backstop against the API returning
something plainly wrong, not a second opinion on its bucketing, so the band is deliberately generous — and
still narrower than half the gap between `16x9` (1.78) and `16x10` (1.60), the closest pair anyone filters
on.
"""

_BATCH_SIZE = "batch_size"
_LIBRARY_PATH = "library_path"
_POOL_TARGET_SIZE = "pool_target_size"
_MIN_WIDTH = "min_width"
_MIN_HEIGHT = "min_height"
_ALLOWED_RATIOS = "allowed_ratios"
_MIN_FAVOURITES = "min_favourites"
"""The `settings` keys. One row per key, with `Settings` as the typed view over them."""


def _default_library_path() -> Path:
    """Where **Favourites** go until the user says otherwise.

    Under Pictures because that is where Windows' own slideshow settings start looking, and in a `wallpapi`
    subfolder because the **Library** is write-only and should never be mixed in with photos.
    """
    return Path.home() / "Pictures" / "wallpapi"


def _defaults() -> dict[str, str]:
    """The seeded value of every setting, as it is stored.

    The single source of truth for what "unconfigured" means: migration 3 seeds from here, and
    `get_settings` falls back to here for a row a hand-edited database has lost. Computed rather than a
    constant because the **Library** default depends on the user's home directory.
    """
    return {
        _BATCH_SIZE: str(DEFAULT_BATCH_SIZE),
        _LIBRARY_PATH: str(_default_library_path()),
        _POOL_TARGET_SIZE: str(DEFAULT_POOL_TARGET_SIZE),
        _MIN_WIDTH: str(DEFAULT_MIN_WIDTH),
        _MIN_HEIGHT: str(DEFAULT_MIN_HEIGHT),
        _ALLOWED_RATIOS: ",".join(DEFAULT_ALLOWED_RATIOS),
        _MIN_FAVOURITES: str(DEFAULT_MIN_FAVOURITES),
    }


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything configurable, as one typed value.

    A plain dataclass, not a Pydantic model: this is inside the core, and Pydantic lives at the edges.
    Storage is still one row per key — this is a view over those rows, so a new setting is a new field and
    a new seeded row rather than a migration against a widening table.
    """

    batch_size: int
    library_path: Path
    pool_target_size: int
    min_width: int
    min_height: int
    allowed_ratios: tuple[str, ...]
    min_favourites: int

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
    """The update did not happen, and this is why.

    A result rather than an exception, in the style of `SubmissionRefused`, so the settings page has one
    error branch and can render the reason next to the value that caused it.
    """

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
        """One reason per **Filter** field rather than the two the batch size has.

        The batch size separates "not a number" from "out of range" because the advice differs: one says
        type a whole number, the other says which numbers are allowed. For the **Filters** both mistakes
        have the same one-sentence answer — the sentence that names the accepted shape — so a second reason
        would be a second branch on the page saying the same thing.
        """

    reason: Reason


@dataclass(frozen=True, slots=True)
class Batch:
    """The `size` **Wallpapers** shown at once, with the identity the submission will quote back.

    `drafts` carries the **Draft Batch** alongside the **Wallpapers**, keyed by **Wallpaper** ID and holding
    only the ones actually marked — absence is an **Ignore**, so there is nothing to store for the rest. It
    is what the page renders its controls from, so a load after a partial draft shows the marks already set.
    """

    id: str
    size: int
    created_at: dt.datetime
    wallpapers: tuple[Wallpaper, ...]
    drafts: Mapping[str, Verdict]


@dataclass(frozen=True)
class BatchUnavailable:
    """No **Batch** could be built. A result rather than an exception, so the UI has one branch.

    Closes #15. Until the **Pool** existed, a transport failure on a **Batch** page load propagated out of
    the Core service and the browser got a 500. Now the page load makes no **API call** at all, so the only
    way to have nothing to show is an empty **Pool** — and the two reasons it can be empty need different
    words on the page. "Nothing has arrived yet" is a matter of waiting; "Wallhaven is unreachable" is not,
    and saying which, and when it last failed, is the difference between a page and a shrug.
    """

    class Reason(StrEnum):
        POOL_EMPTY = "pool_empty"
        """The **Pool** holds nothing the user has not **Banned**, and the refill has not failed."""

        WALLHAVEN_UNREACHABLE = "wallhaven_unreachable"
        """The **Pool** is empty and the last refill attempt failed. `error` says how."""

    reason: Reason
    error: str | None = None
    error_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class RefillStatus:
    """What the background **Pool** refill is doing, for the indicator on the **Batch** page.

    Read through the Core service like everything else, so the page has no second seam to the thread and a
    test can assert on it without starting one.
    """

    pool_size: int
    target_size: int
    running: bool
    last_run_at: dt.datetime | None
    last_error: str | None
    last_error_at: dt.datetime | None

    @property
    def at_target(self) -> bool:
        """Whether the refill is idling rather than spending its budget."""
        return self.pool_size >= self.target_size


@dataclass(frozen=True, slots=True)
class ResolvedVerdict:
    """What one **Wallpaper**'s **Decision log** entries come to.

    Both fields because the callers want different halves: #9 sums `value` across a whole **Pool**, and #7
    renders the `verdict` itself. Never called a **Score** — a **Score** is this spread to similar
    **Wallpapers**, which is #9 and not here.
    """

    verdict: Verdict | None
    value: int


@dataclass(frozen=True, slots=True)
class LibraryReconciliation:
    """What one pass at making the **Library** agree with the **Decision log** did, by **Wallpaper** ID.

    A result rather than nothing, because the pass runs after the **Decision log** has already committed
    and so cannot raise (see `CoreService.reconcile_library`). `failed` is the only way a caller — or a
    test — can tell "there was nothing to do" from "it was tried and it did not work".
    """

    written: tuple[str, ...]
    removed: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class SubmissionRefused:
    """The submission did not happen, and this is why. Never a silent no-op — two tabs is a real case."""

    class Reason(StrEnum):
        UNKNOWN_BATCH = "unknown_batch"
        ALREADY_SUBMITTED = "already_submitted"

    reason: Reason


class _Connections(threading.local):
    """One SQLite connection per thread: `check_same_thread` defaults to `True` and FastAPI's threadpool
    hands out a different thread per request."""

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

        # The refill's own state, held in the process rather than in storage: it describes a thread that
        # exists only while wallpapi is running, and a walk half-finished at shutdown is worth nothing
        # afterwards (a seed reused across runs returns the same **Wallpapers**). The lock is because the
        # refill thread writes it and request threads read it for the indicator.
        self._refill_lock = threading.Lock()
        self._api_call_times: deque[float] = deque(maxlen=CALLS_PER_MINUTE)
        self._walk_seed: str | None = None
        self._walk_page = 1
        self._retry_not_before: float | None = None
        self._refill_last_run: dt.datetime | None = None
        self._refill_last_error: str | None = None
        self._refill_last_error_at: dt.datetime | None = None
        self._refill_thread_running = False

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
        """Apply the numbered steps this database has not seen. Idempotent: a second Core service over the
        same file must find nothing to do rather than assume an empty database."""
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
            write.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- settings --------------------------------------------------------------------------------------

    def get_settings(self) -> Settings:
        """Everything configurable, read in one go.

        Never raises and never refuses. A stored value this Core service would not have accepted cannot
        have been written through `update_settings`, so it is a hand-edited or truncated row; falling back
        to the seeded default keeps the settings page openable, which is where such a row gets fixed.
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
    ) -> Settings | SettingsRefused:
        """Validate and persist the settings named, in one write transaction.

        Keyword-only fields rather than a whole `Settings` value, and `None` meaning "leave this one
        alone". Two reasons. A caller that has to hand back every field must read them all first, and the
        read and the write are then two transactions with a lost update between them. And the page will
        grow sections — **Filters** at #6, **Mixes** at #10 — so a form that renders half the settings must
        not reset the other half by omission. Adding a setting is a new keyword and a new field on
        `Settings`; no existing caller changes. (No setting is nullable today; one that ever is needs a
        sentinel here rather than `None`.)

        Values are accepted as strings as well as typed, because the web form posts strings and the
        coercion rule belongs with the validation rule rather than being spelled out again at the edge.

        Everything is validated before anything is written: a good batch size beside a bad path must not
        half-apply and then be reported as a failure.
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
            # Stored, not created. The **Library** writer creates the folder on its first write (#5);
            # creating it here would leave a folder behind for every path the user typed and undid.
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
            # Stored tidy — trimmed, de-duplicated, in the order given — so the query string Wallhaven
            # sees is the one the settings page shows.
            changes.append((_ALLOWED_RATIOS, ",".join(validated_ratios)))
        if min_favourites is not None:
            validated_favourites = _validated_min_favourites(min_favourites)
            if isinstance(validated_favourites, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_favourites)
            changes.append((_MIN_FAVOURITES, str(validated_favourites)))

        with self._write() as write:
            write.executemany(_UPSERT_SETTING, changes)
            # Read inside the transaction, so what comes back is what this write put there.
            updated = self.get_settings()
            self._prune_pool(write, updated)
            return updated

    # -- batches ---------------------------------------------------------------------------------------

    def get_next_batch(self) -> Batch | BatchUnavailable:
        """The **Batch** waiting to be decided on, sampling the **Pool** only if there isn't one.

        At most one unsubmitted **Batch** exists at a time. Asking again — a page load, a refresh — hands
        back the same one rather than rerolling it, so nothing is stored until there is something to
        decide on.

        **This makes no API call.** The **Wallpapers** are already stored: the background refill put them
        in the **Pool**, filtered, long before anybody asked for a page. That is what closes #15 — there is
        no network call on the page load path left to fail — and it is why the four-call walk this method
        used to do is gone rather than dormant. See ADR 0005.

        A uniform random sample of the **Pool**, minus anything whose resolved **Verdict** is **Ban**.
        **Wallpapers** with other **Verdicts** may reappear; the revisit weight at #11 is what tunes how
        often, and **Zones** and **Mixes** at #9 and #10 replace the uniform draw entirely.

        The batch size is read when a **Batch** is minted and not afterwards, so changing it applies to the
        next **Batch** rather than rebuilding the one on screen and discarding its **Draft Batch**.
        """
        live = self._live_batch()
        if live is not None:
            return live

        size = self.get_settings().batch_size
        candidates = self._without_bans(self._pool_wallpapers())
        if not candidates:
            return self._nothing_to_show()

        chosen = self._random.sample(candidates, min(size, len(candidates)))
        created_at = self._clock.now()
        batch_id = uuid4().hex

        with self._write() as write:
            # Re-read under the write lock (ADR 0002): two tabs opened at once must not each mint a
            # **Batch**. Cheaper than it was — the sampling above is local reads rather than a network
            # call — but the race it closes is the same one.
            contended = _load_live_batch(write)
            if contended is not None:
                return contended
            write.execute(
                "INSERT INTO batches (id, created_at, size) VALUES (?, ?, ?)",
                (batch_id, created_at.isoformat(), len(chosen)),
            )
            write.executemany(
                "INSERT INTO batch_wallpapers (batch_id, wallpaper_id, position) VALUES (?, ?, ?)",
                [(batch_id, w.id, position) for position, w in enumerate(chosen)],
            )

        # A freshly minted **Batch** has nothing marked on it yet.
        return Batch(
            id=batch_id,
            size=len(chosen),
            created_at=created_at,
            wallpapers=tuple(chosen),
            drafts={},
        )

    def _nothing_to_show(self) -> BatchUnavailable:
        """Why the **Pool** had nothing, in the words the page needs (#15).

        A recorded refill failure outranks "nothing yet": if Wallhaven could not be reached, that is the
        fact worth telling the user, and it is the reason the **Pool** never filled.
        """
        with self._refill_lock:
            error, error_at = self._refill_last_error, self._refill_last_error_at
        if error is not None:
            return BatchUnavailable(
                reason=BatchUnavailable.Reason.WALLHAVEN_UNREACHABLE, error=error, error_at=error_at
            )
        return BatchUnavailable(reason=BatchUnavailable.Reason.POOL_EMPTY)

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
            )

    def refill_wait(self) -> float:
        """Seconds the refill should wait before calling `refill_step` again.

        The Core service decides how long; the thread does the waiting, cancellably (invariants 11 and 12).
        Splitting it this way is what lets a test assert the 45-per-minute budget and the back-off with a
        fake clock and no thread at all.

        Three things can ask for a wait and the longest wins: the **Pool** being at target (idle, looking
        again in `IDLE_RECHECK_SECONDS`), the rate limiter, and a back-off a failed call asked for.
        """
        now = self._clock.monotonic()
        if self._pool_size() >= self.get_settings().pool_target_size:
            return IDLE_RECHECK_SECONDS
        with self._refill_lock:
            limited = wait_needed(self._api_call_times, now=now)
            backing_off = 0.0 if self._retry_not_before is None else self._retry_not_before - now
        return max(limited, backing_off, 0.0)

    def refill_step(self) -> None:
        """One step of the refill: at most one **API call**, and never an exception.

        A Core service method rather than something inside the thread, so every test here is a plain
        function call with a fake clock and no thread, no sleeping and no race. The thread (`refill.py`) is
        a loop around this and `refill_wait` and nothing else.

        Never raises, because the thread must not die (#15): a transport failure or a non-200 is recorded
        as the refill's last error, the walk keeps its place so the same page is retried, and the caller
        backs off. The **Pool** being empty because Wallhaven is down is a **Batch unavailable** page, not
        a missing background thread.
        """
        settings = self.get_settings()
        self._mark_refill_run()
        if self._pool_size() >= settings.pool_target_size:
            # At target: the walk is over. The next one starts from a fresh seed, because reusing a seed
            # across walks returns the same **Wallpapers**.
            self._reset_walk()
            return

        with self._refill_lock:
            seed, page_number = self._walk_seed, self._walk_page
            self._api_call_times.append(self._clock.monotonic())
        try:
            page = self._wallhaven.search(
                sorting="random",
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
            # Deliberately everything. The Wallhaven protocol names one error — `RateLimited` — and
            # anything else the client raises is "the call did not happen". Narrowing this to httpx2's
            # exceptions would put the transport's spelling in the Core service and would let one
            # unexpected type kill the thread (#15).
            self._record_refill_failure(failure, ERROR_BACKOFF_SECONDS)
            return

        self._admit_to_pool(page.wallpapers, settings)
        self._advance_walk(page)
        with self._refill_lock:
            self._retry_not_before = None
            self._refill_last_error = None
            self._refill_last_error_at = None

    @contextmanager
    def refill_running(self) -> Generator[None]:
        """Marks the refill as running for as long as the thread's loop is inside this.

        The Core service does not own the thread — the FastAPI lifespan does — so aliveness has to be told
        to it. A context manager rather than a pair of calls, so a loop that dies of something unexpected
        still clears the flag and the indicator says so instead of lying.
        """
        with self._refill_lock:
            self._refill_thread_running = True
        try:
            yield
        finally:
            with self._refill_lock:
                self._refill_thread_running = False

    def _mark_refill_run(self) -> None:
        with self._refill_lock:
            self._refill_last_run = self._clock.now()

    def _record_refill_failure(self, failure: Exception, backoff: float) -> None:
        """Remember why the last **API call** failed, and how long to leave Wallhaven alone.

        The text is `str(failure)` rather than a traceback: it is going on a page for the person who owns
        the machine, and "what went wrong and when" is the whole of what they can act on.
        """
        with self._refill_lock:
            self._refill_last_error = str(failure) or type(failure).__name__
            self._refill_last_error_at = self._clock.now()
            self._retry_not_before = self._clock.monotonic() + backoff

    def _advance_walk(self, page: SearchPage) -> None:
        """Carry `meta.seed` to the next page of this walk, or start a fresh walk on an empty page.

        Wallhaven's random sorting reshuffles on every call unless the seed is passed back, so a walk that
        dropped it would be as likely to hand back page one again as anything new. `meta.last_page` is
        returned but is not a stop condition worth carrying: on a random SFW search it was 14,055 when the
        fixture was captured. An empty page is the real end of a walk.
        """
        with self._refill_lock:
            if not page.wallpapers:
                self._walk_seed, self._walk_page = None, 1
                return
            self._walk_seed = page.seed or self._walk_seed
            self._walk_page += 1

    def _reset_walk(self) -> None:
        with self._refill_lock:
            self._walk_seed, self._walk_page = None, 1

    def _admit_to_pool(self, wallpapers: Sequence[Wallpaper], settings: Settings) -> None:
        """Put the **Wallpapers** that pass every **Filter** into the **Pool**.

        Checked locally against all of them and not only the minimum **Favourites**, which is the one
        Wallhaven cannot do: the API is trusted but not relied upon, and a **Filter** that only exists in a
        query parameter is a **Filter** nothing verifies.

        The `wallpapers` row is upserted and the `pool` row inserted separately, because **Pool**
        membership is its own table: a **Wallpaper** leaves the **Pool** while its **Decision log** entries
        go on referring to it for ever.
        """
        passing = [w for w in _distinct(wallpapers) if _passes_filters(w, settings)]
        if not passing:
            return
        fetched_at = self._clock.now().isoformat()
        with self._write() as write:
            write.executemany(_UPSERT_WALLPAPER, [_wallpaper_row(w) for w in passing])
            write.executemany(_ADMIT_TO_POOL, [(w.id, fetched_at, POOL_SOURCE_RANDOM) for w in passing])

    def _prune_pool(self, write: sqlite3.Connection, settings: Settings) -> None:
        """Drop undecided **Pool** members that no longer pass the **Filters**.

        The rule the **Pool** keeps is "no undecided **Wallpaper** that fails the current **Filters**", so
        this runs after every settings write rather than only after one that named a **Filter** — one rule,
        rather than a rule plus a list of which fields count. With nothing changed there is nothing to
        prune.

        Only the membership row goes. The `wallpapers` row and every **Decision log** entry stay, because
        the log is append-only and **History** at #7 renders a thumbnail for each of them. A **Wallpaper**
        that has been judged keeps its place too: tightening a **Filter** is not a reason to quietly undo a
        decision, and a **Clearance** at #7 would expect to find it.

        The live unsubmitted **Batch** is untouched for free — a **Batch** holds `batch_wallpapers` rows,
        not **Pool** membership — so nobody loses the **Draft Batch** they are part way through.
        """
        rows = write.execute(_SELECT_UNDECIDED_POOL).fetchall()
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

    def _without_bans(self, wallpapers: Sequence[Wallpaper]) -> list[Wallpaper]:
        """Drop the **Wallpapers** whose resolved **Verdict** is **Ban**.

        Resolved rather than merely recorded: a **Ban** that a later **Explicit Verdict** replaces is no
        longer a **Ban**, which is what #7 makes reachable when a past **Verdict** can be changed.
        """
        resolved = self.resolve_verdicts([w.id for w in wallpapers])
        return [w for w in wallpapers if resolved[w.id].verdict is not Verdict.BAN]

    def _live_batch(self) -> Batch | None:
        """The unsubmitted **Batch**, if there is one."""
        return _load_live_batch(self._connect())

    def set_draft_verdict(
        self, batch_id: str, wallpaper_id: str, verdict: Verdict | None
    ) -> SubmissionRefused | None:
        """Mark one tile of the **Draft Batch**, or clear it with `None`.

        Sets rather than toggles (invariant 6): a replayed or duplicated click writes the same row twice
        rather than flipping the state the wrong way round. Clearing deletes the row, because absence
        already means **Ignore** and a stored "none" would be a second way to say the same thing.

        Writes nothing to the **Decision log** — a **Draft Batch** is not the **Decision log**. Drafting
        against an unknown or already submitted **Batch** is refused for the same two reasons submitting
        already uses, so the UI keeps one error branch rather than growing a second.
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

        One operation rather than one per tile (invariant 6). At a **Batch** size of 32 the per-tile
        version is 32 posts and 32 transactions for one click, and it can interleave with an in-flight
        single-tile post and leave the **Draft Batch** half-changed. Here the old rows go and the new ones
        arrive inside one `BEGIN IMMEDIATE`, so nothing can read the **Batch** part-marked.

        The rows are built from what the **Batch** *shows*, not from what is already drafted: "all" means
        every tile on screen, including the ones with no mark yet. `None` deletes every row rather than
        writing explicit nothings, because absence already means **Ignore**.

        Refused for an unknown or already submitted **Batch**, with the reasons a single mark already
        uses. **Ignore** is refused at the web edge exactly as it is for a single mark, so the **Verdict**
        never reaches this method.
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

        The **Draft Batch** supplies the **Explicit Verdicts**; every **Wallpaper** shown and not marked is
        an **Ignore**, derived here rather than stored, because absence already means **Ignore**. Returning
        the next **Batch** is what lets the user keep going without a reload.

        One transaction, and the **Batch** is claimed inside it: the check and the append cannot be split
        by a second browser tab, because `BEGIN IMMEDIATE` takes the write lock before the read.
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
            # Discarded with the submission that consumed it. Left behind, these rows would accumulate
            # against **Batches** that can never be drafted against again (invariant 6).
            write.execute("DELETE FROM draft_batch WHERE batch_id = ?", (batch_id,))
            write.execute("UPDATE batches SET submitted_at = ? WHERE id = ?", (recorded_at, batch_id))

        # Outside the transaction above, deliberately. Downloading a **Favourite** is a network call and no
        # network call belongs inside a write transaction; and a **Favourite** is a fact about taste, not
        # about a download succeeding, so nothing the **Library** does may roll the **Decision log** back.
        # The reconciliation is idempotent, so a failure here is picked up by the next submission.
        self.reconcile_library()
        return self.get_next_batch()

    # -- library ---------------------------------------------------------------------------------------

    def reconcile_library(self) -> LibraryReconciliation:
        """Make the **Library** folder agree with the **Decision log**, and report what that took.

        Derived, not a side effect of a click. Every **Wallpaper** whose resolved **Verdict** is
        **Favourite** and which has no recorded **Library** file gets one; every recorded file whose
        **Wallpaper** is no longer a **Favourite** — replaced by a **Like** or a **Ban** now, **Cleared**
        at #7 — is deleted. Nothing else is touched.

        That makes this idempotent, which is the whole point. Calling it twice writes once. A download that
        fails is not bookkept as failed and retried later; it is simply still a **Favourite** with no file,
        so the next call picks it up. And because the condition is read from the **Decision log** rather
        than from a queue, there is no state that can drift out of step with it.

        Failures are collected rather than raised. This runs at the tail of `submit_batch`, after the
        **Decision log** transaction has committed: a raise here would turn a recorded **Batch** into an
        error page, and the **Verdicts** would be right while the user was told they were not. They are
        reported instead, so a caller that wants to say something can, and so a test can see the
        difference between "nothing to do" and "tried and failed".

        The **Library** is never read back, so a file deleted in Explorer is not noticed and not replaced.
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
                    self._add_to_library(wallpaper_id, str(row["full_url"]), library_path)
                    written.append(wallpaper_id)
                elif not wanted and recorded is not None:
                    self._drop_from_library(wallpaper_id, recorded)
                    removed.append(wallpaper_id)
            except Exception:
                # The writer is across a seam that declares no error type, so there is nothing narrower to
                # catch: a download can fail with anything httpx2 raises and a write with anything the
                # filesystem does. Leaving the row as it was is what makes the next call retry.
                failed.append(wallpaper_id)
        return LibraryReconciliation(written=tuple(written), removed=tuple(removed), failed=tuple(failed))

    def _add_to_library(self, wallpaper_id: str, source_url: str, library_path: Path) -> None:
        """Download one **Favourite** and record where it landed.

        The destination is computed from the **Library** setting as it is *now*, and the path recorded is
        the one the writer returned rather than the one it was handed — the writer is what knows where the
        bytes actually went.
        """
        destination = library_path / f"{wallpaper_id}{_url_suffix(source_url)}"
        written = self._library.write(wallpaper_id, source_url, destination)
        with self._write() as write:
            write.execute(_RECORD_LIBRARY_FILE, (wallpaper_id, str(written), self._clock.now().isoformat()))

    def _drop_from_library(self, wallpaper_id: str, recorded: Path) -> None:
        """Delete a recorded **Library** file and forget it.

        `recorded` comes from the row, never from the current **Library** setting (invariant 9): the path
        is where the file was actually written, and the setting may have changed since. The row goes only
        once the writer has returned, so a failed deletion is retried rather than forgotten about.
        """
        self._library.remove(recorded)
        with self._write() as write:
            write.execute("DELETE FROM library_files WHERE wallpaper_id = ?", (wallpaper_id,))

    # -- thumbnails ------------------------------------------------------------------------------------

    @property
    def thumbnail_dir(self) -> Path:
        """The **Thumbnail cache** directory. Deliberately separate from the **Library**, which is
        favourites-only and write-only."""
        return self._db_path.parent / "thumbnails"

    def get_thumbnail(self, wallpaper_id: str) -> Path | None:
        """The cached thumbnail for a **Wallpaper**, fetching it the first time and never again.

        `None` for a **Wallpaper** this database has never seen. Nothing is evicted at #2; eviction is #7,
        where **History** renders a thumbnail for every past **Verdict**.
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

    # -- verdict resolution ----------------------------------------------------------------------------

    def resolve_verdicts(self, wallpaper_ids: Sequence[str]) -> dict[str, ResolvedVerdict]:
        """**Verdict resolution** for many **Wallpapers** at once, derived and never stored.

        Plural because #9 scores a whole **Pool** in one go, and a per-**Wallpaper** call would force a
        Python loop over 10k rows. Derived on every call because **Scores** are never stored (invariant 2).

        A **Wallpaper** with no entries, or one this database has never seen, resolves to absent and zero.
        """
        requested = list(dict.fromkeys(wallpaper_ids))
        resolved = dict.fromkeys(requested, _ABSENT)
        if not requested:
            return resolved
        # One query rather than one per **Wallpaper**. SQLite's parameter limit is 32,766 here, well above
        # the 10k **Pool** #9 will hand in.
        placeholders = ",".join("?" * len(requested))
        rows = (
            self._connect().execute(_RESOLVE_VERDICTS.format(placeholders=placeholders), requested).fetchall()
        )
        for row in rows:
            resolved[str(row["wallpaper_id"])] = _resolved_from(row["latest_explicit"], int(row["ignores"]))
        return resolved

    # -- history ---------------------------------------------------------------------------------------

    def list_history(self, *, batch_id: str | None = None) -> list[DecisionEntry]:
        """The **Decision log** in sequence order — the order **Verdict resolution** depends on.

        `batch_id` narrows it to one submission. **History** proper is #7; this is the same query with a
        filter, not a second store.
        """
        query = "SELECT seq, wallpaper_id, batch_id, verdict, recorded_at FROM decision_log"
        parameters: tuple[str, ...] = ()
        if batch_id is not None:
            query += " WHERE batch_id = ?"
            parameters = (batch_id,)
        rows = self._connect().execute(f"{query} ORDER BY seq", parameters).fetchall()
        return [
            DecisionEntry(
                seq=int(row["seq"]),
                wallpaper_id=str(row["wallpaper_id"]),
                batch_id=None if row["batch_id"] is None else str(row["batch_id"]),
                verdict=Verdict(row["verdict"]),
                recorded_at=dt.datetime.fromisoformat(str(row["recorded_at"])),
            )
            for row in rows
        ]


def _validated_batch_size(value: int | str) -> int | SettingsRefused.Reason:
    """The batch size, or the reason it is not one.

    Coerces from text because the form posts text. `int` rather than `float`: "1.5" is refused outright
    rather than silently becoming a **Batch** of one, because a size the user did not ask for is worse
    than being told to type a whole number.
    """
    try:
        size = int(str(value).strip())
    except ValueError:
        return SettingsRefused.Reason.BATCH_SIZE_NOT_A_NUMBER
    if not MIN_BATCH_SIZE <= size <= MAX_BATCH_SIZE:
        return SettingsRefused.Reason.BATCH_SIZE_OUT_OF_RANGE
    return size


def _passes_filters(wallpaper: Wallpaper, settings: Settings) -> bool:
    """Every **Filter**, checked locally.

    The same rule admits a **Wallpaper** to the **Pool** and decides whether one already in it has to
    leave, so a **Filter** cannot mean two different things at the two ends.

    Purity is checked here as well as being fixed in the query. `atleast` and `ratios` are checked here as
    well as being sent. The API is trusted but not relied upon: a parameter that was silently ignored, or a
    response shaped differently from the documentation, must not be able to put a NSFW or 1024x768
    **Wallpaper** in front of somebody.
    """
    return (
        wallpaper.purity.strip().lower() == SFW_PURITY_NAME
        and wallpaper.width >= settings.min_width
        and wallpaper.height >= settings.min_height
        and wallpaper.favourites >= settings.min_favourites
        and _matches_an_allowed_ratio(wallpaper, settings.allowed_ratios)
    )


def _matches_an_allowed_ratio(wallpaper: Wallpaper, allowed: Sequence[str]) -> bool:
    """Whether the **Wallpaper**'s own shape is within `RATIO_TOLERANCE` of any allowed ratio.

    Computed from the width and the height rather than read from Wallhaven's `ratio` field, which is
    rounded to two places, and matched with a tolerance because Wallhaven's `ratios=` buckets rather than
    matching exactly — a 3440x1440 ultrawide is 2.39 and it is served under `21x9`.
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
    """`"16x9"` as 1.777…, or `None` if it is not a ratio at all.

    Tolerant of nonsense rather than raising: the settings validator is what refuses an unknown ratio, and
    this one is also reached with whatever a hand-edited row happens to hold.
    """
    width, _, height = named.partition("x")
    try:
        return int(width) / int(height)
    except ValueError, ZeroDivisionError:
        return None


def _whole_number(value: object) -> int | None:
    """The value as a whole number, or `None` if it is not one.

    `int` and never `float`: "1.5" and "1e3" are refused outright rather than silently becoming something
    the user did not type. The form posts text, so the coercion belongs with the rule.
    """
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
    """One axis of the minimum resolution **Filter**, or the reason it is not one.

    Zero is accepted: "no minimum on this axis" is a real answer, and turning a **Filter** off by emptying
    it is better than a second setting saying whether it is on.
    """
    pixels = _whole_number(value)
    if pixels is None or not 0 <= pixels <= MAX_FILTER_PIXELS:
        return reason
    return pixels


def _validated_min_favourites(value: int | str) -> int | SettingsRefused.Reason:
    """The minimum **Favourites** **Filter**, or the reason it is not one.

    No ceiling: unlike a resolution there is no number above which this can only be a typo, and its effect
    — a **Pool** that fills slowly or not at all — is visible on the **Batch** page's refill indicator.
    """
    favourites = _whole_number(value)
    if favourites is None or favourites < 0:
        return SettingsRefused.Reason.MIN_FAVOURITES_INVALID
    return favourites


def _validated_allowed_ratios(value: Sequence[str] | str) -> tuple[str, ...] | SettingsRefused.Reason:
    """The allowed ratios, or the reason they are not.

    Accepted as the comma-separated string the form posts or as a sequence, trimmed and de-duplicated with
    the order kept. Checked against `WALLHAVEN_RATIOS` rather than against being a string, because an
    unrecognised `ratios=` value is not an error Wallhaven reports — it is a search that quietly returns
    something other than what was asked for.

    An empty list is refused: "every ratio" is written as the full list, because a blank field is far more
    likely to be a mistake than an intention.
    """
    parts = value.split(",") if isinstance(value, str) else list(value)
    named = list(dict.fromkeys(part.strip() for part in parts))
    if not named or any(part not in WALLHAVEN_RATIOS for part in named):
        return SettingsRefused.Reason.ALLOWED_RATIOS_INVALID
    return tuple(named)


def _validated_library_path(value: Path | str) -> Path | SettingsRefused.Reason:
    """The **Library** path, or the reason it is not one.

    Absolute or nothing (invariant 9): a relative path moves the **Library** with the working directory,
    and every absolute path recorded for a file written into it would then point somewhere that cannot be
    found again. Nothing here touches the filesystem — the path is allowed not to exist yet, and the
    **Library** writer creates it on its first write (#5).
    """
    text = str(value).strip()
    if not text:
        return SettingsRefused.Reason.LIBRARY_PATH_EMPTY
    path = Path(text)
    if not path.is_absolute():
        return SettingsRefused.Reason.LIBRARY_PATH_NOT_ABSOLUTE
    return path


_UPSERT_SETTING = """
INSERT INTO settings (key, value) VALUES (?, ?)
ON CONFLICT (key) DO UPDATE SET value = excluded.value
"""

_LIBRARY_CANDIDATES = """
SELECT w.id AS wallpaper_id, w.full_url AS full_url, f.path AS path
FROM wallpapers AS w
LEFT JOIN library_files AS f ON f.wallpaper_id = w.id
WHERE f.wallpaper_id IS NOT NULL
   OR w.id IN (SELECT wallpaper_id FROM decision_log WHERE verdict = ?)
ORDER BY w.id
"""
"""Everything a reconciliation could possibly have to do something about, and nothing else.

Two halves, because the work has two directions. A **Wallpaper** can only *resolve* to **Favourite** if it
was **Favourited** at least once, so the **Decision log** half is a superset of what might need writing —
**Verdict resolution** then decides which of them still count. The `library_files` half is everything that
might need deleting. Resolution is not expressed in SQL here: the rule lives in `resolve_verdicts` and
having it in two places is how the two would come to disagree.

Ordered so that what a reconciliation reports is in a fixed order rather than whatever the query planner
felt like — nothing depends on the order of the work itself.
"""

_RECORD_LIBRARY_FILE = """
INSERT INTO library_files (wallpaper_id, path, written_at) VALUES (?, ?, ?)
ON CONFLICT (wallpaper_id) DO UPDATE SET path = excluded.path, written_at = excluded.written_at
"""
"""An upsert rather than an insert.

A write only happens when there is no row — the removal that precedes a re-**Favourite** deletes it — so
the conflict branch is unreachable as things stand. It is here because the alternative to reaching it is an
`IntegrityError` that would be counted as a failed write and retried for ever, and because the row's job is
to say where the file is: the last write wins is the only answer that can be right.
"""

_ABSENT = ResolvedVerdict(verdict=None, value=0)
"""A **Wallpaper** with no **Decision log** entries at all."""

_EXPLICIT_VALUES = {Verdict.FAVOURITE: 100, Verdict.LIKE: 50, Verdict.BAN: -100}
"""What each **Explicit Verdict** resolves to. An **Ignore** is not here — it stacks instead."""

_IGNORE_VALUE = -10
"""One **Ignore**. They stack, but only while the **Wallpaper** has no **Explicit Verdict**."""


def _resolved_from(latest_explicit: object, ignores: int) -> ResolvedVerdict:
    """The resolution rule itself, over one **Wallpaper**'s aggregated entries.

    The latest **Explicit Verdict** wins outright and every **Ignore** on that **Wallpaper** is disregarded,
    before it and after it alike. Only without one do the **Ignores** stack.
    """
    if latest_explicit is not None:
        verdict = Verdict(str(latest_explicit))
        return ResolvedVerdict(verdict=verdict, value=_EXPLICIT_VALUES[verdict])
    if ignores:
        return ResolvedVerdict(verdict=Verdict.IGNORE, value=_IGNORE_VALUE * ignores)
    return _ABSENT


_RESOLVE_VERDICTS = f"""
SELECT
    wallpaper_id,
    (
        SELECT latest.verdict
        FROM decision_log AS latest
        WHERE latest.wallpaper_id = decision_log.wallpaper_id
          AND latest.verdict != '{Verdict.IGNORE.value}'
        ORDER BY latest.seq DESC
        LIMIT 1
    ) AS latest_explicit,
    COUNT(*) FILTER (WHERE verdict = '{Verdict.IGNORE.value}') AS ignores
FROM decision_log
WHERE wallpaper_id IN ({{placeholders}})
GROUP BY wallpaper_id
"""
"""Ordered by `seq` and never by `recorded_at` (invariant 4): every entry from one submit transaction
shares a timestamp, so "the latest **Explicit Verdict**" is only well defined against the sequence."""


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
    """Distinct **Wallpapers** by Wallhaven ID, first occurrence winning.

    A **Batch** is `size` distinct **Wallpapers**, not `size` rows: a random search can return the same one
    more than once.
    """
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
INSERT INTO pool (wallpaper_id, fetched_at, source) VALUES (?, ?, ?)
ON CONFLICT (wallpaper_id) DO NOTHING
"""
"""`DO NOTHING` rather than an upsert: a **Wallpaper** the refill meets again is already in the **Pool**,
and `fetched_at` should stay the moment it first arrived."""

_SELECT_POOL_WALLPAPERS = """
SELECT w.*
FROM pool
JOIN wallpapers AS w ON w.id = pool.wallpaper_id
ORDER BY pool.rowid
"""
"""The whole **Pool**, in a fixed order.

Ordered because the sample is drawn by the seeded random source: without an `ORDER BY`, SQLite's row order
is an implementation detail and the same seed over the same **Pool** could produce a different **Batch**.
"""

_SELECT_UNDECIDED_POOL = """
SELECT w.*
FROM pool
JOIN wallpapers AS w ON w.id = pool.wallpaper_id
WHERE NOT EXISTS (
    SELECT 1 FROM decision_log WHERE decision_log.wallpaper_id = pool.wallpaper_id
)
"""
"""The **Pool** members nothing has ever been recorded against — the only ones a **Filter** change evicts."""

_SELECT_BATCH_WALLPAPERS = """
SELECT w.*
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
"""Select-all as one statement: the **Draft Batch** is built from the rows the **Batch** shows.

Reading `batch_wallpapers` inside the same transaction as the delete is what makes the rewrite atomic. A
Python round trip per **Wallpaper** would be 32 statements for one click and would have to be handed a
list of IDs the caller had read separately, outside the write lock.
"""

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

Not an `executescript`: that commits any transaction already open before it runs, which would take the
migration out of the `BEGIN IMMEDIATE` it is supposed to be inside.
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
"""Migration 2, the **Draft Batch** table.

Keyed `(batch_id, wallpaper_id)`, which enforces one **Verdict** per **Wallpaper** per submission for free.
A separate step rather than an edit to migration 1: that one is already applied to live databases.
"""


_MIGRATION_4 = (
    """
CREATE TABLE library_files (
    wallpaper_id TEXT PRIMARY KEY REFERENCES wallpapers (id),
    path         TEXT NOT NULL,
    written_at   TEXT NOT NULL
)
""",
)
"""Migration 4, the absolute path of every **Library** file written — the deferred decision #4 left here.

Keyed by **Wallpaper**, because the **Library** holds at most one file per **Favourite**. `path` is the
absolute path the writer actually returned, not one derived from the **Library** setting, because that
setting can change and the file does not move with it (invariant 9). `written_at` is display and diagnosis
only; nothing resolves against it, and like every timestamp here it is an ISO 8601 UTC string from the
injected clock (invariant 5).

A row is the one record that a file exists. There is no reading of the folder to check: the **Library** is
write-only, and a file deleted in Explorer is deliberately invisible to wallpapi.
"""


def _migration_3() -> tuple[tuple[str, tuple[str, str]], ...]:
    """Migration 3: seed the settings the settings page edits.

    A function rather than a constant, and statements paired with parameters rather than statements alone,
    because the **Library** default is `Path.home() / "Pictures" / "wallpapi"` — computed here at migration
    time and inserted as text, never interpolated into SQL.

    `DO NOTHING` rather than an upsert: this runs on databases that predate it as well as on empty ones,
    and a database that already holds a chosen `batch_size` must keep it. It also backfills `batch_size`
    into the one shape that could be missing it — a `settings` table hand-edited since migration 1.
    """
    return tuple((_SEED_SETTING, (key, value)) for key, value in _defaults().items())


_SEED_SETTING = "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING"


_CREATE_POOL = """
CREATE TABLE pool (
    wallpaper_id TEXT PRIMARY KEY REFERENCES wallpapers (id),
    fetched_at   TEXT NOT NULL,
    source       TEXT NOT NULL
)
"""
"""**Pool** membership as its own table, never a column on `wallpapers`.

A **Wallpaper** leaves the **Pool** — pruned by a **Filter** change, and evicted by the **Zones** work at
#9 — while the **Decision log** goes on referring to its `wallpapers` row for ever. A `in_pool` column
would make "is it in the **Pool**" and "does this row exist" the same question, and there would be nowhere
to put `fetched_at` or `source` without widening a table that is about the image itself.

`source` is how it got here: `'random'` today, `'like'` when #13 starts searching for lookalikes.
"""


def _migration_5() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 5: the **Pool** table, and the **Filters** and **Pool** target size as seeded rows.

    Paired with parameters for the same reason migration 3 is — the seeds come from `_defaults()`, which is
    computed in Python and inserted as text rather than interpolated into SQL. `DO NOTHING` again, because
    this also runs on databases that already hold settings chosen by hand.

    A step of its own rather than an edit to 3: that one is already applied to live databases.
    """
    return ((_CREATE_POOL, ()), *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()))
