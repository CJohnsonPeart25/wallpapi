"""The SQLite file a **Similarity provider** keeps its own regenerable cache in.

#14's two candidate providers each need somewhere to keep something expensive: Wallhaven tags, which cost
an **API call** apiece, and image embeddings, which cost a model run apiece. Neither belongs in
`wallpapi.db`. That file holds the **Decision log**, which is irreplaceable and accumulates over months; a
tag set or an embedding is derived from a **Wallpaper** that is still on Wallhaven and can be thrown away
and fetched again. Keeping them apart means the spike adds no migration to the one database that matters,
and deleting a provider is deleting a file.

One file per provider rather than one shared one, because the two caches have nothing to say to each other
and a provider that is removed should take its storage with it.

The connection rules are `core.py`'s, and for `core.py`'s reasons (invariant 3): `isolation_level=None` so
nothing implicitly opens a DEFERRED transaction, an explicit `BEGIN IMMEDIATE` for anything that writes so
an upgrading reader cannot take a `SQLITE_BUSY_SNAPSHOT` the busy timeout will not save it from, WAL set
once at creation because it persists in the file, and a connection per thread because FastAPI hands each
request a different one. `synchronous` is left at its default — the point of this file is that losing it
costs a re-fetch, but there is no reason to go out of the way to make that more likely.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path


class _Connections(threading.local):
    """One connection per thread. `check_same_thread` defaults to `True` and the threadpool hands out a
    different thread per request, so a single shared connection would raise the moment the app was used."""

    connection: sqlite3.Connection | None = None


class SidecarDatabase:
    """A small SQLite file beside `wallpapi.db`, owned entirely by one **Similarity provider**.

    `schema` is a tuple of statements applied on first use and on every use after it. They are all
    `CREATE ... IF NOT EXISTS`, so there is no `user_version` and no numbered migration here: a cache whose
    shape has changed is a cache that should be deleted and refilled, which is a one-line answer that a
    migration framework would turn into several.
    """

    def __init__(self, path: Path, schema: Sequence[str]) -> None:
        self._path = path
        self._schema = tuple(schema)
        self._connections = _Connections()
        self._created = False
        self._creation_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def size_bytes(self) -> int:
        """What this cache costs on disk, the write-ahead log included.

        The WAL is counted because it is real disk the cache is using, and a cache just written has most of
        itself in there rather than in the main file.
        """
        total = 0
        for name in (self._path.name, f"{self._path.name}-wal", f"{self._path.name}-shm"):
            candidate = self._path.with_name(name)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    def connect(self) -> sqlite3.Connection:
        connection = self._connections.connection
        if connection is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            self._connections.connection = connection
            self._create(connection)
        return connection

    def _create(self, connection: sqlite3.Connection) -> None:
        """Set WAL and apply the schema, once per process rather than once per thread.

        The lock is not for SQLite's benefit — `CREATE TABLE IF NOT EXISTS` is safe to race — but so that
        two threads opening their first connection at the same moment do not both write the journal mode.
        """
        with self._creation_lock:
            if self._created:
                return
            connection.execute("PRAGMA journal_mode = WAL")
            with self.write(connection) as write:
                for statement in self._schema:
                    write.execute(statement)
            self._created = True

    @contextmanager
    def write(self, connection: sqlite3.Connection | None = None) -> Generator[sqlite3.Connection]:
        """A write transaction, opened with an explicit `BEGIN IMMEDIATE` (invariant 3).

        `connection` is passed in only by `_create`, which is called from inside `connect` and so cannot
        call it again without recursing.
        """
        handle = self.connect() if connection is None else connection
        handle.execute("BEGIN IMMEDIATE")
        try:
            yield handle
        except BaseException:
            handle.execute("ROLLBACK")
            raise
        handle.execute("COMMIT")
