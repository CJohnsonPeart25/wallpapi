"""The **Pool** and its **Refill**: what may enter the **Pool**, and the background search that keeps it
stocked.

The Refill owns its state, its lock, its random source and its rate limiter; nothing outside reads its fields.
The **Pool**'s own storage (admit, prune, retire, size, breakdown, members) is a set of functions taking a
connection, and the **Filters** check and the ratio band live beside them because admitting and pruning share
them (ADR 0005, ADR 0016).
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from wallpapi import decisions, storage
from wallpapi import settings as settings_module
from wallpapi.clock import Clock, iso_utc
from wallpapi.model import Wallpaper
from wallpapi.rng import SeededRandom
from wallpapi.settings import Settings
from wallpapi.wallhaven import REQUEST_TIMEOUT, RateLimited, SearchPage, Wallhaven

SFW_PURITY = "100"
"""Wallhaven's purity mask: SFW on, sketchy and NSFW off. Fixed; NSFW is what needs an API key."""

SFW_PURITY_NAME = "sfw"
"""What a search *result* calls the same thing. The mask is a query parameter; this is a response field."""

ALL_CATEGORIES = "111"
"""Wallhaven's category mask: general, anime and people, all on. The **Filters** are about shape, not
subject.
"""

CALLS_PER_MINUTE = 45

WINDOW_SECONDS = 60.0

IDLE_RECHECK_SECONDS = 30.0
"""How long the refill waits before looking again once the **Pool** is at target: not a spin, not minutes."""

ERROR_BACKOFF_SECONDS = 60.0
"""How long the refill waits after a failed **API call** that named no delay: one whole rate-limit window."""

THREAD_NAME = "wallpapi-refill"

JOIN_TIMEOUT = REQUEST_TIMEOUT + 5.0
"""Seconds shutdown waits for the refill: more than the Wallhaven client's request timeout (invariant 12)."""

LIKE_QUERY_PREFIX = "like:"
"""Wallhaven's spelling of "wallpapers similar to this one", sent as the `q` of a search."""

LIKE_SORTING = "relevance"
"""How a like: search is sorted: most similar first, because the walk is capped and the tail is weak."""

LIKE_PAGES_PER_FAVOURITE = 3
"""How far a like: walk goes before the next **Favourite**'s turn: 72 **Wallpapers**, and the tail is weak."""

RATIO_TOLERANCE = 0.08
"""How far a **Wallpaper**'s own width/height may sit from a named ratio and still count as it.

Wallhaven buckets: a 3440x1440 is 2.39 and served under `21x9`, which is 2.33. Generous on purpose, and still
under half the gap between `16x9` (1.78) and `16x10` (1.60).
"""


class RefillStrategy(StrEnum):
    """Which search a refill step makes, and so the `source` it tags what it admits with: a random walk, or
    a like: search on a **Favourite**.
    """

    RANDOM = "random"
    LIKE = "like"


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
    by_strategy: Mapping[RefillStrategy, int]
    """The **Pool** counted by the strategy that brought each member in: whether like: is feeding it."""

    @property
    def at_target(self) -> bool:
        """Whether the refill is idling rather than spending its budget."""
        return self.pool_size >= self.target_size


def wait_needed(
    call_times: Iterable[float],
    *,
    now: float,
    limit: int = CALLS_PER_MINUTE,
    window: float = WINDOW_SECONDS,
) -> float:
    """Seconds to wait before another **API call** may be made, or zero. A sliding window, not a per-minute
    bucket, over monotonic timestamps: it says how long and never waits itself.
    """
    inside = sorted(t for t in call_times if now - t < window)
    if len(inside) < limit:
        return 0.0
    # The call that must age out leaves `limit - 1` behind it: not the oldest if the limit was lowered.
    ages_out = inside[len(inside) - limit]
    return max(0.0, ages_out + window - now)


@dataclass(frozen=True, slots=True)
class _Walk:
    """Where one walk has got to. `subject` is a like: walk's **Favourite**, and `None` for the random one."""

    subject: str | None = None
    seed: str | None = None
    page: int = 1


_FRESH = _Walk()


class Refill:
    """The background search that keeps the **Pool** stocked, one **API call** a step.

    The thread drives `wait` and `step`; the page reads `status`. Its state lives in memory, because a walk
    half-finished at shutdown is worth nothing afterwards, and behind one lock, because the refill thread
    writes it while request threads read it. No SQL runs and no **API call** is made with the lock held.
    """

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        wallhaven: Wallhaven,
        clock: Clock,
        random_source: SeededRandom,
    ) -> None:
        self._connect = connect
        self._wallhaven = wallhaven
        self._clock = clock
        self._random = random_source

        self._lock = threading.Lock()
        self._call_times: deque[float] = deque(maxlen=CALLS_PER_MINUTE)
        """The **API call** timestamps still inside the limiter's window.

        Trimmed by age on every append, and capped by `maxlen` as well: only the cap holds when the clock does
        not move.
        """
        # The two walks are kept apart: they interleave, and would clobber a shared place.
        self._walk = _FRESH
        self._like_walk = _FRESH
        self._like_walked: frozenset[str] = frozenset()
        """The **Favourites** already walked this cycle, so every one has a turn before any has a second."""
        self._last_strategy: RefillStrategy | None = None
        self._retry_not_before: float | None = None
        self._last_run: dt.datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: dt.datetime | None = None
        self._running = False

    def status(self) -> RefillStatus:
        """What the refill is doing: its own fields as one snapshot, beside the **Pool** counted outside the
        lock.
        """
        connection = self._connect()
        pool_size = size(connection)
        by_strategy = breakdown(connection)
        target_size = settings_module.get(connection).pool_target_size
        with self._lock:
            return RefillStatus(
                pool_size=pool_size,
                target_size=target_size,
                running=self._running,
                last_run_at=self._last_run,
                last_error=self._last_error,
                last_error_at=self._last_error_at,
                last_strategy=self._last_strategy,
                by_strategy=by_strategy,
            )

    def wait(self) -> float:
        """Seconds before the next `step`: the longest of idling at target, the limiter and a back-off."""
        now = self._clock.monotonic()
        connection = self._connect()
        if size(connection) >= settings_module.get(connection).pool_target_size:
            return IDLE_RECHECK_SECONDS
        with self._lock:
            limited = wait_needed(self._call_times, now=now)
            backing_off = 0.0 if self._retry_not_before is None else self._retry_not_before - now
        return max(limited, backing_off, 0.0)

    def step(self) -> None:
        """One step: at most one **API call**, and never an exception from it: the thread must not die.

        The random and like: strategies take strict turns; with no **Favourites** every step is random. A
        failure is recorded, the walk keeps its place, and `wait` backs off.
        """
        connection = self._connect()
        current = settings_module.get(connection)
        with self._lock:
            self._last_run = self._clock.now()
        if size(connection) >= current.pool_target_size:
            # At target: the random walk is over, and the next starts from a fresh seed. The like: walk keeps
            # its place: `like:<id>` has no seed trap.
            with self._lock:
                self._walk = _FRESH
            return

        favourites = decisions.favourites(connection)
        with self._lock:
            strategy = _alternated(self._last_strategy, has_favourites=bool(favourites))
            self._last_strategy = strategy
            if strategy is RefillStrategy.LIKE:
                self._like_walk, self._like_walked = _take_up_a_like_walk(
                    self._like_walk, self._like_walked, favourites, self._random
                )
                walk = self._like_walk
            else:
                walk = self._walk
            _note_call(self._call_times, self._clock.monotonic())
        try:
            page = self._wallhaven.search(
                sorting="random" if walk.subject is None else LIKE_SORTING,
                query=None if walk.subject is None else f"{LIKE_QUERY_PREFIX}{walk.subject}",
                purity=SFW_PURITY,
                categories=ALL_CATEGORIES,
                page=walk.page,
                seed=walk.seed,
                atleast=current.atleast,
                ratios=current.ratios,
            )
        except RateLimited as limited:
            # Wallhaven's own answer beats the default: it knows when it will start answering again.
            self._failed(limited, limited.retry_after or ERROR_BACKOFF_SECONDS)
            return
        except Exception as failure:
            # Deliberately everything: the protocol names only `RateLimited`, and one unexpected type must
            # not kill the thread.
            self._failed(failure, ERROR_BACKOFF_SECONDS)
            return

        with storage.write(connection) as write:
            admit(write, page.wallpapers, current, source=strategy, at=self._clock.now())
        with self._lock:
            if strategy is RefillStrategy.LIKE:
                self._like_walk, self._like_walked = _advance_like_walk(
                    self._like_walk, self._like_walked, page
                )
            else:
                self._walk = _advance_walk(self._walk, page)
            self._retry_not_before = None
            self._last_error = None
            self._last_error_at = None

    @contextmanager
    def running(self) -> Generator[None]:
        """Marks the refill as running while the thread's loop is inside this, and clears it however the loop
        ends.
        """
        with self._lock:
            self._running = True
        try:
            yield
        finally:
            with self._lock:
                self._running = False

    def _failed(self, failure: Exception, backoff: float) -> None:
        """Remember why the last **API call** failed, for the page, and how long to leave Wallhaven alone."""
        with self._lock:
            self._last_error = str(failure) or type(failure).__name__
            self._last_error_at = self._clock.now()
            self._retry_not_before = self._clock.monotonic() + backoff


def refill_loop(refill: Refill, stop_event: threading.Event) -> None:
    """Wait as long as the Refill says, take one step, repeat. `step` never raises, so the thread cannot
    die.
    """
    with refill.running():
        while not stop_event.is_set():
            wait = refill.wait()
            if wait > 0 and stop_event.wait(wait):
                return
            if stop_event.is_set():
                return
            refill.step()


def _note_call(call_times: deque[float], at: float) -> None:
    """Note an **API call** and drop those aged out of the window, so `wait_needed` stays a pure function."""
    while call_times and at - call_times[0] >= WINDOW_SECONDS:
        call_times.popleft()
    call_times.append(at)


def _alternated(last: RefillStrategy | None, *, has_favourites: bool) -> RefillStrategy:
    """Whichever strategy did not take the last step, while both have work: strict turns are an even split."""
    if not has_favourites or last is RefillStrategy.LIKE:
        return RefillStrategy.RANDOM
    return RefillStrategy.RANDOM if last is None else RefillStrategy.LIKE


def similar_groups(similarities: NDArray[np.float32], radius: float) -> list[list[int]]:
    """The lookalike subjects grouped by taste, as indices into a square subjects x subjects matrix
    (ADR 0022).

    Greedy leader clustering, in the given order: each subject joins the first leader it is within `radius`
    of, by the **Score**'s distance `1 - similarity`, or leads a group of its own. Leaders, not connected
    components, because near neighbours chain: a group is at most `2 * radius` across.
    """
    groups: list[list[int]] = []
    for subject in range(similarities.shape[0]):
        joined = next((g for g in groups if similarities[subject, g[0]] >= 1 - radius), None)
        if joined is None:
            groups.append([subject])
        else:
            joined.append(subject)
    return groups


def _advance_walk(walk: _Walk, page: SearchPage) -> _Walk:
    """Carry `meta.seed` to the next page of this walk, or start a fresh walk on an empty page."""
    if not page.wallpapers:
        return _FRESH
    return replace(walk, seed=page.seed or walk.seed, page=walk.page + 1)


def _take_up_a_like_walk(
    walk: _Walk, walked: frozenset[str], favourites: Sequence[str], random_source: SeededRandom
) -> tuple[_Walk, frozenset[str]]:
    """The like: walk the next search makes, and the **Favourites** walked this cycle.

    Carries on with the walk in progress while its subject is still a **Favourite**; otherwise starts on one
    that has not had a turn this cycle, so every **Favourite** gets one.
    """
    if walk.subject is not None and walk.subject in favourites:
        return walk, walked
    walked = walked.intersection(favourites)
    remaining = [f for f in favourites if f not in walked]
    if not remaining:
        walked, remaining = frozenset[str](), list(favourites)
    return _Walk(subject=random_source.sample(remaining, 1)[0]), walked


def _advance_like_walk(walk: _Walk, walked: frozenset[str], page: SearchPage) -> tuple[_Walk, frozenset[str]]:
    """Page on through one **Favourite**'s lookalikes until an empty page or `LIKE_PAGES_PER_FAVOURITE`."""
    if page.wallpapers and walk.page < LIKE_PAGES_PER_FAVOURITE:
        return replace(walk, seed=page.seed or walk.seed, page=walk.page + 1), walked
    if walk.subject is not None:
        walked = walked | {walk.subject}
    return _FRESH, walked


# -- the Pool's own storage ------------------------------------------------------------------------------


def admit(
    write: sqlite3.Connection,
    wallpapers: Sequence[Wallpaper],
    current: Settings,
    *,
    source: RefillStrategy,
    at: dt.datetime,
) -> None:
    """Put the **Wallpapers** that pass every **Filter** into the **Pool**, inside the caller's write
    transaction, checked locally whatever was asked.

    A **Wallpaper** already in the **Pool** keeps the `source` and `fetched_at` it arrived with. One the
    **Decision log** mentions is refused (ADR 0016), though its `wallpapers` row is still refreshed.
    `fetched_at` is `at` as an ISO 8601 UTC string (invariant 5); a naive moment is refused.
    """
    fetched_at = iso_utc(at)
    passing = [w for w in _distinct(wallpapers) if _passes_filters(w, current)]
    if not passing:
        return
    write.executemany(_UPSERT_WALLPAPER, [_wallpaper_row(w) for w in passing])
    write.executemany(
        _ADMIT_TO_POOL, [{"id": w.id, "fetched_at": fetched_at, "source": source.value} for w in passing]
    )


def prune(write: sqlite3.Connection, current: Settings) -> None:
    """Drop every **Pool** member that no longer passes the **Filters**; whoever updates the settings calls it
    in the same transaction.

    Membership only: the `wallpapers` row and the **Decision log** stay, and the live **Batch** is untouched.
    """
    retire(write, [w.id for w in members(write) if not _passes_filters(w, current)])


def retire(write: sqlite3.Connection, wallpaper_ids: Sequence[str]) -> None:
    """Drop these **Wallpapers** from the **Pool** inside the caller's transaction: what a submitted **Batch**
    showed, decided once (ADR 0016). Membership only, as with `prune`.
    """
    write.executemany("DELETE FROM pool WHERE wallpaper_id = ?", [(w,) for w in wallpaper_ids])


def size(connection: sqlite3.Connection) -> int:
    """How many **Wallpapers** the **Pool** holds."""
    return int(connection.execute("SELECT COUNT(*) FROM pool").fetchone()[0])


def breakdown(connection: sqlite3.Connection) -> dict[RefillStrategy, int]:
    """How many **Pool** members each strategy brought in, every strategy a key. Counted from the `source`
    column, not kept in memory: a count in memory restarts at zero and counts pages, not **Wallpapers**.

    A `source` no strategy spells is left out rather than raised on: every **Batch** page renders this.
    """
    counted = {str(row[0]): int(row[1]) for row in connection.execute(_COUNT_BY_SOURCE)}
    return {strategy: counted.get(strategy.value, 0) for strategy in RefillStrategy}


def contains(connection: sqlite3.Connection, wallpaper_id: str) -> bool:
    """Whether the **Wallpaper** is in the **Pool** now."""
    return (
        connection.execute("SELECT 1 FROM pool WHERE wallpaper_id = ?", (wallpaper_id,)).fetchone()
        is not None
    )


def members(connection: sqlite3.Connection) -> list[Wallpaper]:
    """Every **Wallpaper** in the **Pool**, ordered so a seeded random source draws the same sample."""
    return [wallpaper_from_row(row) for row in connection.execute(_SELECT_POOL_WALLPAPERS)]


def wallpapers(connection: sqlite3.Connection, wallpaper_ids: Sequence[str]) -> dict[str, Wallpaper]:
    """Every **Wallpaper** named that this database has recorded, in the **Pool** or not, by id."""
    placeholders = ",".join("?" * len(wallpaper_ids))
    rows = connection.execute(f"SELECT * FROM wallpapers WHERE id IN ({placeholders})", list(wallpaper_ids))
    return {str(row["id"]): wallpaper_from_row(row) for row in rows}


def decided(connection: sqlite3.Connection) -> list[Wallpaper]:
    """Every **Wallpaper** the **Decision log** mentions, in or out of the **Pool**, in a fixed order.

    Whole **Wallpapers** because the **Similarity provider** is handed them; ordered so the matrix's columns,
    and so every **Score**, are reproducible.
    """
    return [wallpaper_from_row(row) for row in connection.execute(_SELECT_DECIDED_WALLPAPERS)]


def wallpaper_from_row(row: sqlite3.Row) -> Wallpaper:
    """A `wallpapers` row as a **Wallpaper**; the row may carry extra columns. `pool` writes that table, so
    this is where it is read back, for `batches` and **History** too."""
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


def _passes_filters(wallpaper: Wallpaper, current: Settings) -> bool:
    """Every **Filter**, checked locally: the one rule for admitting to the **Pool** and for pruning it.

    Purity, `atleast` and `ratios` are checked here as well as sent: the API is trusted but not relied upon.
    """
    return (
        wallpaper.purity.strip().lower() == SFW_PURITY_NAME
        and wallpaper.width >= current.min_width
        and wallpaper.height >= current.min_height
        and wallpaper.favourites >= current.min_favourites
        and _matches_an_allowed_ratio(wallpaper, current.allowed_ratios)
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


_ADMIT_TO_POOL = f"""
INSERT INTO pool (wallpaper_id, fetched_at, source)
SELECT :id, :fetched_at, :source
WHERE :id NOT IN ({decisions.MENTIONED})
ON CONFLICT (wallpaper_id) DO NOTHING
"""
"""Admit unless the **Decision log** mentions it at all, a legacy `cleared` entry included (ADR 0016).

`DO NOTHING`, so `fetched_at` stays the first arrival.
"""

_COUNT_BY_SOURCE = "SELECT source, COUNT(*) FROM pool GROUP BY source"

_SELECT_DECIDED_WALLPAPERS = f"""
SELECT w.*
FROM wallpapers AS w
WHERE w.id IN ({decisions.MENTIONED})
ORDER BY w.id
"""

_SELECT_POOL_WALLPAPERS = """
SELECT w.*
FROM pool
JOIN wallpapers AS w ON w.id = pool.wallpaper_id
ORDER BY pool.rowid
"""
"""The whole **Pool**, in a fixed order, so a seeded draw is reproducible."""

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
