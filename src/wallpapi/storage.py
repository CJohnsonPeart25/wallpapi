"""The only way to open a SQLite connection, and the only place migrations live.

Every rule below was verified in this project against SQLite 3.50.4; each sits beside the line that keeps it.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from wallpapi import evidence_log, settings
from wallpapi.taste.judgement import Judgement, Shown, instant
from wallpapi.taste.verdict_grid import VERDICT_GRID_V0, LegacyEntry, LegacyTileResponse

SCHEMA_VERSION = 17
"""The last numbered step. Steps apply by number, in order, so the numbers stay contiguous."""


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """A connection with every rule applied. Register no `sqlite3` adapters: timestamps are ISO 8601 UTC
    strings the caller writes. `read_only` opens a file that is already there and refuses every write on it:
    it never creates one, and never migrates, checkpoints or writes the one it opens.
    """
    # Legacy transaction control emits a DEFERRED `BEGIN` on its own; `write` says when a transaction starts.
    # `timeout` is the busy timeout, left at its default 5 seconds.
    # `check_same_thread` left on: one connection per thread, held by `ThreadConnections`.
    # `mode=ro` in a URI, whose path `as_uri` percent-encodes. Reading a WAL file that nobody else has open,
    # SQLite makes the empty `-wal` and the `-shm` index beside it, which a reader cannot remove.
    target = f"{Path(path).resolve().as_uri()}?mode=ro" if read_only else path
    connection = sqlite3.connect(target, isolation_level=None, uri=read_only)
    connection.row_factory = sqlite3.Row
    # Per-connection and not persisted: a second connection reads 0 until it turns them on.
    connection.execute("PRAGMA foreign_keys = ON")
    # `synchronous` stays at its default FULL, never NORMAL: in WAL, NORMAL loses recent commits on power
    # loss, and the Evidence log is irreplaceable.
    return connection


class NotCurrent(Exception):
    """A database `connect_current` will not open: none at `path` (`version` is `None`), or one at a
    `user_version` other than `SCHEMA_VERSION`."""

    def __init__(self, path: Path, version: int | None) -> None:
        super().__init__(path, version)
        self.path = path
        self.version = version


def connect_current(path: Path) -> sqlite3.Connection:
    """A `read_only` connection to a database already at `SCHEMA_VERSION`, for a command that only reads. It
    creates nothing and migrates nothing, so it is safe beside a server of any version; any other database
    raises `NotCurrent`."""
    if not path.is_file():
        raise NotCurrent(path, None)
    connection = connect(path, read_only=True)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        connection.close()
        raise NotCurrent(path, version)
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


@contextmanager
def read(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """One read transaction: every read inside sees one snapshot, so a write committed meanwhile on another
    connection lands before all of them or after all of them, never between two. Ended however the block
    ends, so the connection is handed back ready for `write`.

    For reads that must agree with each other, outside any transaction: `BEGIN` refuses to nest. DEFERRED,
    so it waits for no lock and takes its snapshot at the first read. Nothing is written inside: a reader
    that turns writer is refused at once (see `write`).
    """
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        connection.execute("COMMIT")


@contextmanager
def read_only(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """`read` on a connection SQLite holds to reading: any write on it is refused. Handed back able to
    write. For a command that must be safe beside a running server."""
    connection.execute("PRAGMA query_only = ON")
    try:
        with read(connection):
            yield connection
    finally:
        connection.execute("PRAGMA query_only = OFF")


class ThreadConnections(threading.local):
    """One connection per thread to one file: the threadpool hands each request a different thread."""

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self._path = path
        self._read_only = read_only
        self._connection: sqlite3.Connection | None = None

    def get(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = connect(self._path, read_only=self._read_only)
        return self._connection


type _Statement = tuple[str, tuple[str | int, ...]]

type _Step = _Statement | Callable[[sqlite3.Connection], None]
"""One part of a migration: a statement with its parameters, or a function handed the migration's write
handle, for a step SQL alone cannot take."""


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
                for step in statements:
                    if callable(step):
                        step(handle)
                    else:
                        statement, parameters = step
                        handle.execute(statement, parameters)
        handle.execute(f"PRAGMA user_version = {target}")


def _steps() -> tuple[tuple[_Step, ...], ...]:
    """Every step's statements, in order: the first entry is migration 1. Built per run, because the steps
    that re-seed take today's defaults.
    """
    seeded = settings.seeds()
    seed_settings = tuple((_SEED_SETTING, (key, value)) for key, value in seeded.items())
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
            *((_SEED_MIX, (mix.name, mix.unknown, mix.banger, mix.dud)) for mix in settings.DEFAULT_MIXES),
            *seed_settings,
        ),
        # 8: seeded the **Revisit weight**. That setting is gone, so on a fresh database this seeds nothing
        # new, and migration 10 deletes the row.
        seed_settings,
        # 9: the **Similarity radius** default moves from 0.5 to 0.15 (ADR 0013), only where untouched.
        (
            (
                _RETUNE_SETTING,
                (seeded[_SIMILARITY_RADIUS], _SIMILARITY_RADIUS, str(settings.SUPERSEDED_SIMILARITY_RADIUS)),
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
                (seeded[_POOL_TARGET_SIZE], _POOL_TARGET_SIZE, str(settings.SUPERSEDED_POOL_TARGET_SIZE)),
            ),
            *seed_settings,
        ),
        # 11: the **Similarity radius** default moves from 0.15 to 0.10 (ADR 0023), only where untouched.
        (
            (
                _RETUNE_SETTING,
                (seeded[_SIMILARITY_RADIUS], _SIMILARITY_RADIUS, str(settings.RETUNED_SIMILARITY_RADIUS)),
            ),
            *seed_settings,
        ),
        # 12: the **Evidence log** (ADR 0027): one row per **Judgement** and one per shown **Item**,
        # append-only by trigger, and the **Mix** a **Batch** was minted from. The **Decision log** is
        # converted into it and left as it was, unread.
        (*((statement, ()) for statement in _MIGRATION_12), _convert_decision_log, *seed_settings),
        # 13: what a **Batch** records beyond its tiles: the **Zone** each **Slot** was meant for, the
        # **Ranking stamp** it was drawn under, and which tiles' previews were opened on its **Draft Batch**.
        (*((statement, ()) for statement in _MIGRATION_13), *seed_settings),
        # 14: the **Line-up** on screen, and a place in the **Evidence log** for the **Response** fields no
        # SQL reads; the **Line-up** size is seeded with the other settings.
        (*((statement, ()) for statement in _MIGRATION_14), *seed_settings),
        # 15: the stars held against the live **Line-up** while it is live.
        (*((statement, ()) for statement in _MIGRATION_15), *seed_settings),
        # 16: the ranks of an **Order** in progress, held against the live **Line-up** while it is live.
        (*((statement, ()) for statement in _MIGRATION_16), *seed_settings),
        # 17: a **Gauntlet**'s champion and its streak, held on its tile of the live **Line-up**.
        (*((statement, ()) for statement in _MIGRATION_17), *seed_settings),
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


_MIGRATION_12 = (
    """
CREATE TABLE judgements (
    id                 TEXT PRIMARY KEY,
    format_version     INTEGER NOT NULL,
    instrument         TEXT NOT NULL,
    instrument_version INTEGER NOT NULL,
    screen             TEXT NOT NULL,
    recorded_at        TEXT NOT NULL,
    utc_offset         INTEGER,
    first_shown_at     TEXT,
    app                TEXT NOT NULL,
    app_version        TEXT NOT NULL,
    submitted          INTEGER,
    ranking            TEXT,
    context            TEXT NOT NULL
)
""",
    """
CREATE TABLE judgement_items (
    judgement_id   TEXT NOT NULL REFERENCES judgements (id),
    position       INTEGER NOT NULL,
    item           TEXT NOT NULL,
    verdict        TEXT,
    favourite      INTEGER NOT NULL,
    preview_opened INTEGER NOT NULL,
    entry          TEXT,
    provenance     TEXT NOT NULL,
    PRIMARY KEY (judgement_id, position),
    UNIQUE (judgement_id, item)
)
""",
    "CREATE INDEX judgement_items_by_item ON judgement_items (item)",
    """
CREATE TRIGGER judgement_items_name_a_known_wallpaper
BEFORE INSERT ON judgement_items
WHEN NEW.item LIKE 'wallhaven:%'
    AND NOT EXISTS (SELECT 1 FROM wallpapers WHERE id = substr(NEW.item, length('wallhaven:') + 1))
BEGIN
    SELECT RAISE(ABORT, 'FOREIGN KEY constraint failed: no such wallpaper');
END
""",
    *(
        f"""
CREATE TRIGGER {table}_{change.lower()}_refused
BEFORE {change} ON {table}
BEGIN
    SELECT RAISE(ABORT, 'the Evidence log is append-only');
END
"""
        for table in ("judgements", "judgement_items")
        for change in ("UPDATE", "DELETE")
    ),
    "ALTER TABLE batches ADD COLUMN mix TEXT",
)
"""Migration 12's schema, the **Evidence log** (ADR 0027).

`judgements` holds what a **Judgement** says once for the screen: `recorded_at` and `first_shown_at` as
`taste.judgement.instant` text, so text order is time order; `utc_offset` in whole seconds; `submitted` the
verdict grid's judgement-level **Response**, `NULL` for an **Instrument** with none; the **Ranking stamp**
(`NULL` when nothing ranked the set) and the context as JSON text. `judgement_items` holds each shown **Item**
by its namespaced key, with the per-**Item** **Response** in the columns SQL reads (`verdict`, `favourite`,
`preview_opened` for version 1, `entry` for version 0's converted words) and **Provenance** as JSON text.

The **Decision log**'s foreign key to `wallpapers` becomes a trigger, because a key is not a bare id: a
**History** edit of a **Wallpaper** this database never recorded is refused by the database, as before. Both
tables refuse an update or a delete outright. `batches.mix` is `NULL` for a **Batch** minted before it.
"""


def _convert_decision_log(write: sqlite3.Connection) -> None:
    """Migration 12's conversion: the **Decision log** into **Judgements**, once (ADR 0027). Reads
    `decision_log`, `batches` and `batch_wallpapers` and changes none of them.

    Each submitted **Batch** (the entries sharing a `batch_id`) becomes one verdict grid version 0
    **Judgement** on the batch **Screen**: its entries in on-screen order (an entry with no tile row after
    the placed ones, by sequence), each in its old words with the stored **Zone** as **Provenance** where
    there is one, the **Batch**'s mint time as first-shown, and the latest of its entries' times as its
    recorded time. Each entry with no **Batch** becomes a **History** **Judgement** of one **Item**.

    Order by sequence becomes order by time. Taken in old sequence order, each **Judgement** keeps its time
    unless an earlier one on an **Item** it shares is at or after it, and then moves to one microsecond after
    the latest such: on a plain tie that is ADR 0027's one microsecond, and a move that makes a new tie, or a
    time the clock wrote out of order, moves on as far as it must. So on every **Item** the time order is the
    old sequence order, and each resolves as it did. Nothing here depends on the database it runs in, so two
    copies of one log convert to the same ids.

    The ids are `taste/`'s hash of the canonical form, format version 1, whose spelling a test pins; a change
    to it is a new format version, which `Judgement` refuses to build at 1. The rows are written by the
    statements below rather than `evidence_log.append`, so a later migration that changes the tables cannot
    change what this one writes.
    """
    placed = {
        (str(row["batch_id"]), str(row["wallpaper_id"])): (int(row["position"]), row["zone"])
        for row in write.execute("SELECT batch_id, wallpaper_id, position, zone FROM batch_wallpapers")
    }
    minted = {
        str(row["id"]): str(row["created_at"]) for row in write.execute("SELECT id, created_at FROM batches")
    }
    groups: dict[tuple[str, str | int], list[sqlite3.Row]] = {}
    for row in write.execute(
        "SELECT seq, wallpaper_id, batch_id, verdict, recorded_at FROM decision_log ORDER BY seq"
    ):
        group = ("entry", int(row["seq"])) if row["batch_id"] is None else ("batch", str(row["batch_id"]))
        groups.setdefault(group, []).append(row)

    latest: dict[str, dt.datetime] = {}
    for rows in groups.values():
        wallpaper_ids = [str(row["wallpaper_id"]) for row in rows]
        at = max(dt.datetime.fromisoformat(str(row["recorded_at"])) for row in rows)
        at = max([at, *(latest[w] + evidence_log.ONE_MICROSECOND for w in wallpaper_ids if w in latest)])
        batch_id = rows[0]["batch_id"]
        judgement = (
            _converted_entry(rows[0], at)
            if batch_id is None
            else _converted_batch(str(batch_id), rows, at, placed, minted)
        )
        _insert_converted(write, judgement)
        latest.update(dict.fromkeys(wallpaper_ids, at))


def _converted_entry(row: sqlite3.Row, at: dt.datetime) -> Judgement:
    """One entry with no **Batch**: a **History** edit, or a legacy **Clearance**."""
    return Judgement(
        instrument=VERDICT_GRID_V0,
        screen=evidence_log.SCREEN_HISTORY,
        shown=(_converted_tile(row, {}),),
        recorded_at=at,
        app=evidence_log.APP,
        app_version=evidence_log.APP_VERSION,
    )


def _converted_batch(
    batch_id: str,
    rows: list[sqlite3.Row],
    at: dt.datetime,
    placed: dict[tuple[str, str], tuple[int, object]],
    minted: dict[str, str],
) -> Judgement:
    """One submitted **Batch**'s entries, in on-screen order."""
    unplaced = len(placed) + 1

    def on_screen(row: sqlite3.Row) -> tuple[int, int]:
        return placed.get((batch_id, str(row["wallpaper_id"])), (unplaced, None))[0], int(row["seq"])

    def provenance(row: sqlite3.Row) -> dict[str, str]:
        zone = placed.get((batch_id, str(row["wallpaper_id"])), (unplaced, None))[1]
        return {} if zone is None else {evidence_log.PROVENANCE_ZONE: str(zone)}

    created = minted.get(batch_id)
    return Judgement(
        instrument=VERDICT_GRID_V0,
        screen=evidence_log.SCREEN_BATCH,
        shown=tuple(_converted_tile(row, provenance(row)) for row in sorted(rows, key=on_screen)),
        recorded_at=at,
        app=evidence_log.APP,
        app_version=evidence_log.APP_VERSION,
        first_shown_at=None if created is None else dt.datetime.fromisoformat(created),
    )


def _converted_tile(row: sqlite3.Row, provenance: dict[str, str]) -> Shown:
    return Shown(
        evidence_log.item_key(str(row["wallpaper_id"])),
        LegacyTileResponse(LegacyEntry(str(row["verdict"]))),
        provenance,
    )


def _insert_converted(write: sqlite3.Connection, judgement: Judgement) -> None:
    """Migration 12's rows for one converted **Judgement**, in the columns the tables had then."""
    write.execute(
        "INSERT INTO judgements (id, format_version, instrument, instrument_version, screen, recorded_at,"
        " utc_offset, first_shown_at, app, app_version, submitted, ranking, context)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, '{}')",
        (
            judgement.id,
            judgement.format_version,
            judgement.instrument.name,
            judgement.instrument.version,
            judgement.screen,
            instant(judgement.recorded_at),
            None if judgement.first_shown_at is None else instant(judgement.first_shown_at),
            judgement.app,
            judgement.app_version,
        ),
    )
    write.executemany(
        "INSERT INTO judgement_items (judgement_id, position, item, verdict, favourite, preview_opened,"
        " entry, provenance) VALUES (?, ?, ?, NULL, 0, 0, ?, ?)",
        [
            (
                judgement.id,
                position,
                shown.item,
                _legacy_entry(shown),
                json.dumps(dict(shown.provenance), sort_keys=True),
            )
            for position, shown in enumerate(judgement.shown)
        ],
    )


def _legacy_entry(shown: Shown) -> str:
    response = shown.response
    if not isinstance(response, LegacyTileResponse):
        raise TypeError(f"migration 12 converts only legacy tiles, not {response!r}")
    return response.entry.value


_MIGRATION_13 = (
    "ALTER TABLE batch_wallpapers ADD COLUMN slot_zone TEXT",
    "ALTER TABLE batches ADD COLUMN ranking TEXT",
    """
CREATE TABLE draft_preview (
    batch_id     TEXT NOT NULL REFERENCES batches (id),
    wallpaper_id TEXT NOT NULL REFERENCES wallpapers (id),
    PRIMARY KEY (batch_id, wallpaper_id)
)
""",
)
"""Migration 13's schema. `slot_zone` is the **Zone** a tile's **Slot** was meant for, beside `zone`, the one
it was drawn from; `NULL` for a **Batch** minted before it. `ranking` is the **Ranking stamp** as the
**Evidence log** keeps it, JSON text, `NULL` when nothing ranked the **Batch** or it was minted before this.
`draft_preview` is the half of the **Draft Batch** that sits beside a tile's one mark: a row per tile whose
preview was opened, set and never cleared, and dropped with the rest of the **Draft Batch**.
"""


_MIGRATION_14 = (
    "ALTER TABLE judgements ADD COLUMN response TEXT",
    "ALTER TABLE judgement_items ADD COLUMN response TEXT",
    """
CREATE TABLE lineups (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    mix        TEXT NOT NULL,
    ranking    TEXT
)
""",
    """
CREATE TABLE lineup_wallpapers (
    lineup_id    TEXT NOT NULL REFERENCES lineups (id),
    wallpaper_id TEXT NOT NULL REFERENCES wallpapers (id),
    position     INTEGER NOT NULL,
    zone         TEXT NOT NULL,
    slot_zone    TEXT NOT NULL,
    PRIMARY KEY (lineup_id, wallpaper_id)
)
""",
)
"""Migration 14's schema.

The two `response` columns are a **Response**'s fields that no SQL reads, as JSON text by field name: a
**Pick** tile's `picked` and its screen's `none_of_these`, and whatever **Order** and **Gauntlet** add. A
field SQL reads keeps a column of its own, as a **Pick** tile's `favourite` does in `favourite`. `NULL` for
the verdict grid, whose fields all have columns, and for every row from before.

`lineups` and `lineup_wallpapers` hold the **Line-up** waiting to be answered, as `batches` and
`batch_wallpapers` hold a **Batch**: its mint time, **Mix** and **Ranking stamp** (JSON text, `NULL` when
nothing ranked it), and each tile in on-screen order with the **Zone** it was drawn from and its **Slot**'s.
A **Line-up** is deleted when it is answered, its **Judgement** then holding all of it, or by a **Pool
reset**.
"""


_MIGRATION_15 = (
    """
CREATE TABLE lineup_stars (
    lineup_id    TEXT NOT NULL,
    wallpaper_id TEXT NOT NULL,
    PRIMARY KEY (lineup_id, wallpaper_id),
    FOREIGN KEY (lineup_id, wallpaper_id) REFERENCES lineup_wallpapers (lineup_id, wallpaper_id)
)
""",
)
"""Migration 15's schema: a row per starred tile of the live **Line-up**, as `draft_preview` is a row per
opened preview beside a **Batch**. Keyed to the tile, not to an **Instrument**, so every **Instrument** on a
**Line-up** holds its stars the same way. Set and cleared, never toggled, and dropped with the **Line-up**
when it is answered (the **Judgement** then holding each star as a **Favourite**), replaced by a visit (ADR
0029) or discarded.
"""


_MIGRATION_16 = (
    """
CREATE TABLE lineup_ranks (
    lineup_id    TEXT NOT NULL,
    wallpaper_id TEXT NOT NULL,
    rank         INTEGER NOT NULL CHECK (rank >= 1),
    PRIMARY KEY (lineup_id, wallpaper_id),
    UNIQUE (lineup_id, rank),
    FOREIGN KEY (lineup_id, wallpaper_id) REFERENCES lineup_wallpapers (lineup_id, wallpaper_id)
)
""",
)
"""Migration 16's schema: a row per ranked tile of the live **Line-up**, an **Order** in progress, beside
`lineup_stars` and keyed to the tile the same way. Holds the ranks as the screen shows them, 1 to k: taking
one back rewrites the rest closed up, so no gap and no order of clicks is kept. Dropped with the **Line-up**
when it is answered (the **Judgement** then holding each rank), replaced by a visit (ADR 0029) or
discarded.
"""


_MIGRATION_17 = (
    "ALTER TABLE lineup_wallpapers ADD COLUMN streak INTEGER CHECK (streak IS NULL OR streak >= 1)",
    "CREATE UNIQUE INDEX lineup_champion ON lineup_wallpapers (lineup_id) WHERE streak IS NOT NULL",
)
"""Migration 17's schema: a **Gauntlet**'s champion, the tile it kept from the screen before, is the one tile
of the live **Line-up** with a `streak`, how many screens in a row it has won; every drawn tile has `NULL`.
At most one per **Line-up**, by the partial unique index. On the tile rather than on `lineups`, so it keeps
its place by the tile's own `position` and goes with the tile: dropped when the **Line-up** is answered (the
next being drawn around the winner), replaced by a visit (ADR 0029) or discarded by a **Pool reset**.
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


_SIMILARITY_RADIUS = settings.Settings.similarity_radius.key
_POOL_TARGET_SIZE = settings.Settings.pool_target_size.key
"""The two keys a migration retunes where the seeded value is untouched."""


_REVISIT_WEIGHT_KEY = "revisit_weight"
"""The `settings` key migration 8 seeded, kept only so migration 10 can delete the row (ADR 0016)."""
