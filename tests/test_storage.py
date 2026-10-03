"""The storage rules, through `storage` alone: a real database, no `compose`.

The contention tests use two connections to one file, one of them on a thread, and wait on events with a
timeout rather than sleeping.
"""

from __future__ import annotations

import ast
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from tests.conftest import SOURCE
from wallpapi import storage

TABLES = {
    "batch_wallpapers",
    "batches",
    "decision_log",
    "draft_batch",
    "library_files",
    "mixes",
    "pool",
    "settings",
    "wallpapers",
}


@pytest.fixture
def memory() -> Iterator[sqlite3.Connection]:
    with closing(storage.connect(":memory:")) as connection:
        yield connection


@pytest.fixture
def migrated(db_path: Path) -> Path:
    with closing(storage.connect(db_path)) as connection:
        storage.migrate(connection)
    return db_path


def _schema(connection: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    rows = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    return [(str(row["type"]), str(row["name"]), row["sql"]) for row in rows]


def _contents(connection: sqlite3.Connection) -> tuple[list[tuple[str, ...]], ...]:
    settings = connection.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    mixes = connection.execute("SELECT name, unknown, banger, dud FROM mixes ORDER BY name").fetchall()
    return [tuple(row) for row in settings], [tuple(row) for row in mixes]


def _version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def test_migrating_in_two_stops_builds_what_one_run_builds(memory: sqlite3.Connection) -> None:
    storage.migrate(memory, to=8)
    assert _version(memory) == 8
    assert {name for kind, name, _ in _schema(memory) if kind == "table"} >= TABLES
    assert "decision_log_by_wallpaper" not in {name for _, name, _ in _schema(memory)}

    storage.migrate(memory)

    with closing(storage.connect(":memory:")) as at_once:
        storage.migrate(at_once)
        assert _version(memory) == _version(at_once) == storage.SCHEMA_VERSION
        assert _schema(memory) == _schema(at_once)
        assert _contents(memory) == _contents(at_once)
    assert "decision_log_by_wallpaper" in {name for _, name, _ in _schema(memory)}


def test_a_second_migration_over_one_file_changes_nothing(migrated: Path) -> None:
    with closing(storage.connect(migrated)) as connection:
        before = _schema(connection), _contents(connection)
        storage.migrate(connection)

        assert (_schema(connection), _contents(connection)) == before


def test_a_fresh_connection_has_every_rule_applied(memory: sqlite3.Connection) -> None:
    assert memory.isolation_level is None
    assert memory.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert memory.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_wal_persists_in_the_file_for_every_later_connection(migrated: Path) -> None:
    with closing(storage.connect(migrated)) as second:
        assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_failed_write_leaves_nothing_behind(migrated: Path) -> None:
    with closing(storage.connect(migrated)) as connection:
        with pytest.raises(RuntimeError), storage.write(connection) as handle:
            handle.execute("INSERT INTO settings (key, value) VALUES ('half', 'written')")
            raise RuntimeError

        assert connection.execute("SELECT * FROM settings WHERE key = 'half'").fetchall() == []
        assert not connection.in_transaction


def test_a_reader_that_turns_writer_fails_at_once_despite_the_busy_timeout(migrated: Path) -> None:
    """Why `write` opens with `BEGIN IMMEDIATE`: a DEFERRED transaction that read before another connection
    committed gets `SQLITE_BUSY_SNAPSHOT` on its first write, with the busy handler skipped."""
    with closing(storage.connect(migrated)) as reader, closing(storage.connect(migrated)) as other:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM settings").fetchone()
        with storage.write(other) as handle:
            handle.execute("INSERT INTO settings (key, value) VALUES ('other', '1')")

        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            reader.execute("INSERT INTO settings (key, value) VALUES ('reader', '1')")

        assert time.monotonic() - started < 1.0  # the busy timeout is 5 seconds
        reader.execute("ROLLBACK")


def test_write_waits_for_a_held_lock_and_then_succeeds(migrated: Path) -> None:
    """`write` never fails fast on contention: it waits out the other writer, and the holder's own
    read-then-write goes through meanwhile."""
    done = threading.Event()
    failures: list[BaseException] = []

    def waiter() -> None:
        try:
            with closing(storage.connect(migrated)) as connection, storage.write(connection) as handle:
                handle.execute("INSERT INTO settings (key, value) VALUES ('waiter', '1')")
        except BaseException as failure:  # reported below, on the test's own thread
            failures.append(failure)
        finally:
            done.set()

    with closing(storage.connect(migrated)) as holder:
        thread = threading.Thread(target=waiter)
        with storage.write(holder) as handle:
            handle.execute("SELECT COUNT(*) FROM settings").fetchone()
            thread.start()
            assert not done.wait(0.2)
            handle.execute("INSERT INTO settings (key, value) VALUES ('holder', '1')")
        assert done.wait(5)
        thread.join()

        assert failures == []
        keys = {str(row["key"]) for row in holder.execute("SELECT key FROM settings")}
        assert {"holder", "waiter"} <= keys


def _sqlite_calls(path: Path, *names: str) -> list[int]:
    """Lines calling `sqlite3.<name>`, or passing `detect_types`, which is how adapters get switched on.
    Parsed, not grepped: a docstring may say it."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            lines.append(node.lineno)
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = (
            isinstance(func, ast.Attribute)
            and func.attr in names
            and isinstance(func.value, ast.Name)
            and func.value.id == "sqlite3"
        )
        if called or any(keyword.arg == "detect_types" for keyword in node.keywords):
            lines.append(node.lineno)
    return lines


def _offenders(*names: str) -> dict[str, list[int]]:
    return {
        path.relative_to(SOURCE).as_posix(): lines
        for path in sorted(SOURCE.rglob("*.py"))
        if (lines := _sqlite_calls(path, *names))
    }


def test_storage_is_the_only_place_a_connection_is_opened() -> None:
    offenders = _offenders("connect")

    assert list(offenders) == ["storage.py"]
    assert len(offenders["storage.py"]) == 1


def test_no_source_file_registers_a_sqlite3_adapter() -> None:
    """Timestamps are ISO 8601 UTC strings the caller writes; nothing converts them on the way in or out."""
    assert _offenders("register_adapter", "register_converter") == {}


def test_the_sqlite3_guard_sees_every_spelling(tmp_path: Path) -> None:
    source = tmp_path / "adapted.py"
    source.write_text(
        '"""Never `sqlite3.register_adapter`."""\n'
        "import sqlite3\nfrom sqlite3 import connect\n"
        "sqlite3.register_adapter(int, str)\nsqlite3.register_converter('x', str)\n"
        "sqlite3.connect(':memory:', detect_types=1)\nsqlite3.connect(':memory:')\n",
        encoding="utf-8",
    )

    assert _sqlite_calls(source, "register_adapter", "register_converter") == [3, 4, 5, 6]
