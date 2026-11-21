"""`sql.select_in`: one bound parameter per id, in as many statements as the connection's own limit needs.

A plain table on an in-memory connection, so what is counted is the helper's chunking and nothing of any
module's SQL.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing

import pytest

from wallpapi import sql, storage

IDS = [f"id{n:02d}" for n in range(10)]


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    with closing(storage.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE things (id TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO things (id) VALUES (?)", [(i,) for i in IDS])
        yield connection


def _things(marks: str) -> str:
    return f"SELECT id FROM things WHERE id IN ({marks})"


def _statements(connection: sqlite3.Connection) -> list[str]:
    sent: list[str] = []
    connection.set_trace_callback(sent.append)
    return sent


def test_more_values_than_the_connection_binds_go_in_chunks_under_its_limit(
    connection: sqlite3.Connection,
) -> None:
    """Lowered to 3, eleven ids take four statements and every recorded row comes back; SQLite itself refuses
    a statement binding a fourth, so none did."""
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 3)
    sent = _statements(connection)

    rows = sql.select_in(connection, [*IDS, "absent"], _things)

    assert sorted(str(row["id"]) for row in rows) == IDS
    assert len(sent) == 4


def test_a_value_asked_twice_is_bound_once_even_across_chunks(connection: sqlite3.Connection) -> None:
    """First seen first: the repeats of `id00` and `id01` would otherwise fall in a later chunk and come back
    a second time."""
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)

    rows = sql.select_in(connection, ["id00", "id01", "id02", "id00", "id03", "id01"], _things)

    assert [str(row["id"]) for row in rows] == ["id00", "id01", "id02", "id03"]


def test_no_values_send_no_statement(connection: sqlite3.Connection) -> None:
    sent = _statements(connection)

    assert sql.select_in(connection, [], _things) == []
    assert sent == []
