"""The only way to open a SQLite connection, and the only place migrations live.

Every rule below was verified in this project against SQLite 3.50.4; each sits beside the line that keeps it.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 10
"""The last numbered step. Steps chain `if applied < n` in order, so the numbers stay contiguous."""


def connect(path: Path | str) -> sqlite3.Connection:
    """A connection with every rule applied. Register no `sqlite3` adapters: timestamps are ISO 8601 UTC
    strings the caller writes.
    """
    # Legacy transaction control emits a DEFERRED `BEGIN` on its own; `write` says when a transaction starts.
    # `timeout` is the busy timeout, left at its default 5 seconds.
    # `check_same_thread` left on: one connection per thread, held by `ThreadConnections`.
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # Per-connection and not persisted: a second connection reads 0 until it turns them on.
    connection.execute("PRAGMA foreign_keys = ON")
    # `synchronous` stays at its default FULL, never NORMAL: in WAL, NORMAL loses recent commits on power
    # loss, and the Decision log is irreplaceable.
    return connection


@contextmanager
def write(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """A write transaction, opened with an explicit `BEGIN IMMEDIATE`; commits, or rolls back on exception.

    A transaction that starts as a reader and upgrades to a writer gets `SQLITE_BUSY_SNAPSHOT` with the busy
    handler skipped, so `busy_timeout` would not save it. `BEGIN IMMEDIATE` takes the lock first, and waits
    for it.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


class ThreadConnections(threading.local):
    """One connection per thread to one file: the threadpool hands each request a different thread."""

    def __init__(self, path: Path | str) -> None:
        self._path = path
        self._connection: sqlite3.Connection | None = None

    def get(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = connect(self._path)
        return self._connection


type _Statement = tuple[str, tuple[str | int, ...]]


def migrate(connection: sqlite3.Connection, to: int | None = None) -> None:
    """Apply the numbered steps after this database's `user_version`, up to `to` (default: every step).

    A second run over one file finds nothing to do.
    """
    target = SCHEMA_VERSION if to is None else to
    applied = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if applied >= target:
        return
    # Persists in the file, so it is set once here rather than per connection. Cannot run in a transaction.
    connection.execute("PRAGMA journal_mode = WAL")
    with write(connection) as handle:
        for number, statements in enumerate(_steps(), start=1):
            if applied < number <= target:
                for statement, parameters in statements:
                    handle.execute(statement, parameters)
        handle.execute(f"PRAGMA user_version = {target}")


def _steps() -> tuple[tuple[_Statement, ...], ...]:
    """Every step's statements, in order: the first entry is migration 1. Built per run, because the steps
    that re-seed take today's defaults.
    """
    # Deferred: `core` imports this module. Until the settings module takes the seeds out of `core`.
    from wallpapi.core import (
        _POOL_TARGET_SIZE,  # pyright: ignore[reportPrivateUsage]
        _SIMILARITY_RADIUS,  # pyright: ignore[reportPrivateUsage]
        DEFAULT_MIXES,
        SUPERSEDED_POOL_TARGET_SIZE,
        SUPERSEDED_SIMILARITY_RADIUS,
        _defaults,  # pyright: ignore[reportPrivateUsage]
    )

    seed_settings = tuple((_SEED_SETTING, (key, value)) for key, value in _defaults().items())
    return (
        tuple((statement, ()) for statement in _MIGRATION_1),
        tuple((statement, ()) for statement in _MIGRATION_2),
        # 3: seed the settings. `DO NOTHING`, so a value already chosen is kept.
        seed_settings,
        tuple((statement, ()) for statement in _MIGRATION_4),
        # 5: the **Pool** table, and the **Filters** and **Pool** target size as seeded rows.
        ((_CREATE_POOL, ()), *seed_settings),
        # 6: the **Zone** a **Batch** drew each **Wallpaper** from, and the similarity settings. `zone` is a
        # fact about the **Batch**, not a stored **Score**. NULL for a **Batch** minted before it.
        (("ALTER TABLE batch_wallpapers ADD COLUMN zone TEXT", ()), *seed_settings),
        # 7: **Mixes** as a table, seeded with **Explore** and **Refine**, and the active one as a setting.
        (
            (_CREATE_MIXES, ()),
            *((_SEED_MIX, (mix.name, mix.unknown, mix.banger, mix.dud)) for mix in DEFAULT_MIXES),
            *seed_settings,
        ),
        # 8: seeded the **Revisit weight**. That setting is gone, so on a fresh database this seeds nothing
        # new, and migration 10 deletes the row.
        seed_settings,
        # 9: the **Similarity radius** default moves from 0.5 to 0.15 (ADR 0013), only where untouched.
        (
            (
                _RETUNE_SETTING,
                (_defaults()[_SIMILARITY_RADIUS], _SIMILARITY_RADIUS, str(SUPERSEDED_SIMILARITY_RADIUS)),
            ),
            *seed_settings,
        ),
        # 10: decide once (ADR 0016). The **Pool** gives up everything the **Decision log** mentions; the log
        # is indexed by **Wallpaper** for admission; the **Revisit weight** row goes; the **Pool** target
        # moves from 2000 to 500 where untouched. A **Pool** above its new target drains rather than being
        # trimmed.
        (
            ("DELETE FROM pool WHERE wallpaper_id IN (SELECT wallpaper_id FROM decision_log)", ()),
            ("CREATE INDEX IF NOT EXISTS decision_log_by_wallpaper ON decision_log (wallpaper_id)", ()),
            ("DELETE FROM settings WHERE key = ?", (_REVISIT_WEIGHT_KEY,)),
            (
                _RETUNE_SETTING,
                (_defaults()[_POOL_TARGET_SIZE], _POOL_TARGET_SIZE, str(SUPERSEDED_POOL_TARGET_SIZE)),
            ),
            *seed_settings,
        ),
    )


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


_RETUNE_SETTING = "UPDATE settings SET value = ? WHERE key = ? AND value = ?"
"""Change a setting only where it still holds the value a previous migration seeded, so a choice survives."""


_REVISIT_WEIGHT_KEY = "revisit_weight"
"""The `settings` key migration 8 seeded, kept only so migration 10 can delete the row (ADR 0016)."""
