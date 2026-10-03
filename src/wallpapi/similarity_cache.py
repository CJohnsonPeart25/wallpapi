"""The SQLite file a **Similarity provider** keeps its own regenerable cache in, apart from the **Decision
log**.

One file per provider, so removing a provider is deleting a file. Connection rules are `CoreService._write`'s.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path


class _Connections(threading.local):
    """One connection per thread: the threadpool hands each request a different thread."""

    connection: sqlite3.Connection | None = None


class SidecarDatabase:
    """A small SQLite file beside `wallpapi.db`, owned by one **Similarity provider**.

    `schema` is applied on every first use and is all `CREATE ... IF NOT EXISTS`: a cache whose shape changed
    is deleted, so no migration.
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
        """What this cache costs on disk, write-ahead log included."""
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
        """Set WAL and apply the schema once per process, under a lock."""
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
        """A write transaction: see `CoreService._write`."""
        handle = self.connect() if connection is None else connection
        handle.execute("BEGIN IMMEDIATE")
        try:
            yield handle
        except BaseException:
            handle.execute("ROLLBACK")
            raise
        handle.execute("COMMIT")
