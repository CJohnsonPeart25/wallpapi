"""The Core service: the single seam between the UI and everything else.

Invariant 1 — the UI and every test talk only to this class, and its five dependencies are injected.
Storage lives here too: SQLite is an in-process detail of the Core service, not another seam.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from wallpapi.clock import Clock
from wallpapi.library import LibraryWriter
from wallpapi.model import DecisionEntry, Verdict, Wallpaper
from wallpapi.rng import SeededRandom
from wallpapi.similarity import SimilarityProvider
from wallpapi.wallhaven import WallhavenSearcher

SCHEMA_VERSION = 1

SFW_PURITY = "100"
"""Wallhaven's purity mask, most significant bit first: SFW on, sketchy and NSFW off."""


@dataclass(frozen=True, slots=True)
class Batch:
    """The `size` **Wallpapers** shown at once, with the identity the submission will quote back."""

    id: str
    size: int
    created_at: dt.datetime
    wallpapers: tuple[Wallpaper, ...]


@dataclass(frozen=True)
class BatchUnavailable:
    """No **Batch** could be built. A result rather than an exception, so the UI has one branch."""

    class Reason(StrEnum):
        NO_RESULTS = "no_results"

    reason: Reason


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
        wallhaven: WallhavenSearcher,
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
        """Build a **Batch** from a live Wallhaven random, SFW search.

        There is no **Pool** at #2, so every **Batch** is its own search and consecutive ones may repeat.
        """
        page = self._wallhaven.search(sorting="random", purity=SFW_PURITY, page=1)
        candidates = _distinct(page.wallpapers)
        if not candidates:
            return BatchUnavailable(reason=BatchUnavailable.Reason.NO_RESULTS)

        size = int(self.get_setting("batch_size"))
        chosen = self._random.sample(candidates, min(size, len(candidates)))
        created_at = self._clock.now()
        batch_id = uuid4().hex

        with self._write() as write:
            write.executemany(_UPSERT_WALLPAPER, [_wallpaper_row(w) for w in chosen])
            write.execute(
                "INSERT INTO batches (id, created_at, size) VALUES (?, ?, ?)",
                (batch_id, created_at.isoformat(), len(chosen)),
            )
            write.executemany(
                "INSERT INTO batch_wallpapers (batch_id, wallpaper_id, position) VALUES (?, ?, ?)",
                [(batch_id, w.id, position) for position, w in enumerate(chosen)],
            )

        return Batch(id=batch_id, size=len(chosen), created_at=created_at, wallpapers=tuple(chosen))

    def submit_batch(self, batch_id: str) -> Batch | BatchUnavailable | SubmissionRefused:
        """Append the **Batch**'s **Verdicts** to the **Decision log**, then hand back the next **Batch**.

        Nothing is picked at #2 — **Draft Batches** arrive at #3 — so every **Wallpaper** shown is an
        **Ignore**. Returning the next **Batch** is what lets the user keep going without a reload.

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
            write.executemany(
                "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, ?, ?, ?)",
                [(row["wallpaper_id"], batch_id, Verdict.IGNORE.value, recorded_at) for row in shown],
            )
            write.execute("UPDATE batches SET submitted_at = ? WHERE id = ?", (recorded_at, batch_id))

        return self.get_next_batch()

    # -- history ---------------------------------------------------------------------------------------

    def list_history(self) -> list[DecisionEntry]:
        """The **Decision log** in sequence order — the order **Verdict resolution** depends on."""
        rows = (
            self._connect()
            .execute(
                "SELECT seq, wallpaper_id, batch_id, verdict, recorded_at FROM decision_log ORDER BY seq"
            )
            .fetchall()
        )
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
