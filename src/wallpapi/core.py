"""The Core service: the single seam between the UI and everything else.

Invariant 1 — the UI and every test talk only to this class, and its five dependencies are injected.
Storage lives here too: SQLite is an in-process detail of the Core service, not another seam.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

from wallpapi.clock import Clock
from wallpapi.library import LibraryWriter
from wallpapi.model import DecisionEntry, Verdict, Wallpaper
from wallpapi.rng import SeededRandom
from wallpapi.similarity import SimilarityProvider
from wallpapi.wallhaven import Wallhaven

SCHEMA_VERSION = 2

SFW_PURITY = "100"
"""Wallhaven's purity mask, most significant bit first: SFW on, sketchy and NSFW off."""

MAX_SEARCHES_PER_BATCH = 4
"""How many **API calls** one **Batch** may cost while somebody waits for the page.

A bound on page load latency, not a rate limiter: four calls at the client's ten second timeout is already
the worst case worth making a user sit through. Spending the documented 45 per minute is the background
refill's job at #6.
"""


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
    """No **Batch** could be built. A result rather than an exception, so the UI has one branch."""

    class Reason(StrEnum):
        NO_RESULTS = "no_results"

    reason: Reason


@dataclass(frozen=True, slots=True)
class ResolvedVerdict:
    """What one **Wallpaper**'s **Decision log** entries come to.

    Both fields because the callers want different halves: #9 sums `value` across a whole **Pool**, and #7
    renders the `verdict` itself. Never called a **Score** — a **Score** is this spread to similar
    **Wallpapers**, which is #9 and not here.
    """

    verdict: Verdict | None
    value: int


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
            write.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- settings --------------------------------------------------------------------------------------

    def get_setting(self, key: str) -> str:
        row = self._connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        """Change a setting. The settings page is #4; the settings themselves exist from #2, so #4 adds a
        page rather than a code path."""
        with self._write() as write:
            write.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- batches ---------------------------------------------------------------------------------------

    def get_next_batch(self) -> Batch | BatchUnavailable:
        """The **Batch** waiting to be decided on, minting one from Wallhaven only if there isn't one.

        At most one unsubmitted **Batch** exists at a time. Asking again — a page load, a refresh — hands
        back the same one rather than rerolling it, so nothing is stored until there is something to
        decide on. There is no **Pool** at #2, so each new **Batch** is its own live search.
        """
        live = self._live_batch()
        if live is not None:
            return live

        size = int(self.get_setting("batch_size"))
        candidates = self._gather_candidates(size)
        if not candidates:
            return BatchUnavailable(reason=BatchUnavailable.Reason.NO_RESULTS)

        chosen = self._random.sample(candidates, min(size, len(candidates)))
        created_at = self._clock.now()
        batch_id = uuid4().hex

        with self._write() as write:
            # Re-read under the write lock: two tabs opened at once must not each mint a Batch, and the
            # search above deliberately happened outside the transaction rather than holding the lock
            # across a network call.
            contended = _load_live_batch(write)
            if contended is not None:
                return contended
            write.executemany(_UPSERT_WALLPAPER, [_wallpaper_row(w) for w in chosen])
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

    def _gather_candidates(self, size: int) -> list[Wallpaper]:
        """Enough un-**Banned** **Wallpapers** for a **Batch**, walking pages until there are.

        **Banned Wallpapers** are excluded permanently, which can leave a page short of `size`. Rather than
        ship a **Batch** thinned by past **Bans**, walk on to the next page carrying Wallhaven's returned
        `meta.seed` — random sorting reshuffles on every call without it, so page two would be as likely to
        repeat page one as to bring anything new.

        A walk that ends still short ships what it has: a smaller **Batch** is an acceptable outcome, and
        only finding nothing at all is a **Batch unavailable**.

        Temporary by design. When the **Pool** lands at #6, **Batch** building reads locally stored
        **Wallpapers** that a background thread has already topped up, no **API call** happens on the page
        load path, and this method is deleted rather than unpicked.
        """
        gathered: list[Wallpaper] = []
        seen: set[str] = set()
        seed: str | None = None
        for call in range(1, MAX_SEARCHES_PER_BATCH + 1):
            if call == 1:
                # Deliberately not guarded. A transport failure on the first call is issue #15, and
                # widening the rescue below to cover it would close that ticket by accident.
                page = self._wallhaven.search(sorting="random", purity=SFW_PURITY, page=call)
            else:
                try:
                    page = self._wallhaven.search(sorting="random", purity=SFW_PURITY, page=call, seed=seed)
                except Exception:
                    # The pages already gathered are worth more than the one that failed: end the walk and
                    # ship them rather than losing a **Batch** that was nearly built. The protocol declares
                    # no error type, so there is nothing narrower to catch from this side of the seam.
                    break
            seed = page.seed or seed
            if not page.wallpapers:
                break
            fresh = [w for w in _distinct(page.wallpapers) if w.id not in seen]
            seen.update(w.id for w in fresh)
            gathered.extend(self._without_bans(fresh))
            if len(gathered) >= size:
                break
        return gathered

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

        return self.get_next_batch()

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
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(destination, data)
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


def _write_atomically(destination: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, then `os.replace`.

    The temp file must be a sibling: `os.replace` is only atomic within one filesystem and raises across
    drives on Windows. A half-written file must never be servable.
    """
    temporary = destination.with_name(f"{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


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


_SELECT_BATCH_WALLPAPERS = """
SELECT w.*
FROM batch_wallpapers AS bw
JOIN wallpapers AS w ON w.id = bw.wallpaper_id
WHERE bw.batch_id = ?
ORDER BY bw.position
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
