"""Binding a list of ids SQLite could refuse in one statement; shared by every module that asks about many
**Wallpapers** at once.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable


def select_in(
    connection: sqlite3.Connection, values: Iterable[str], query: Callable[[str], str]
) -> list[sqlite3.Row]:
    """Every row `query` selects for `values`, one bound parameter each, in as few statements as the
    connection's own parameter limit allows.

    `query` is handed one chunk's placeholders (`?,?,?`) and returns SQL whose only parameters they are.
    Values are deduplicated, first seen first, so none is asked about in two chunks; no values send no
    statement. The limit is read from the connection on each call, never assumed: 32,766 by default, but a
    build or a test may lower it. Several statements, so a caller that needs one snapshot reads inside a
    transaction.
    """
    requested = list(dict.fromkeys(values))
    size = connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    rows: list[sqlite3.Row] = []
    for start in range(0, len(requested), size):
        chunk = requested[start : start + size]
        rows.extend(connection.execute(query(",".join("?" * len(chunk))), chunk).fetchall())
    return rows
