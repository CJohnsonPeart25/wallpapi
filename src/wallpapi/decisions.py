"""The **Decision log**: appending to it, resolving it and paging it. The one piece of data wallpapi cannot
rebuild, and nothing else reads or writes `decision_log`.

The table and its index are made by `storage`'s migrations; every query over them is here.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from wallpapi.model import Clearance, DecisionEntry, Verdict

HISTORY_PAGE_SIZE = 100
"""Rows on one page of **History**: it grows by thousands of **Ignores** a week and needs *a* bound."""


@dataclass(frozen=True, slots=True)
class ResolvedVerdict:
    """What one **Wallpaper**'s **Decision log** entries come to: the `verdict` to show and the `value` to
    score.
    """

    verdict: Verdict | None
    value: int


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One **Wallpaper**'s line in **History**, not one log entry: what it resolves to, and when the
    deciding entry was made.
    """

    wallpaper_id: str
    resolved: ResolvedVerdict
    decided_at: dt.datetime


@dataclass(frozen=True, slots=True)
class History:
    """One page of **History**. `total` counts every line the filter matches, not only this page's."""

    entries: tuple[HistoryEntry, ...]
    page: int
    pages: int
    total: int

    @property
    def previous_page(self) -> int | None:
        return self.page - 1 if self.page > 1 else None

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.page < self.pages else None


MENTIONED = "SELECT wallpaper_id FROM decision_log"
"""Every **Wallpaper** with any entry at all, a legacy **Clearance** included, as a subquery for SQL elsewhere
to compose: what the **Pool** refuses (ADR 0016). Asked of the log, not of resolution, because a **Clearance**
resolves to the same nothing as a **Wallpaper** never seen.
"""


def append(
    write: sqlite3.Connection, verdicts: Mapping[str, Verdict], *, batch_id: str | None, at: dt.datetime
) -> None:
    """Append one entry per **Wallpaper**, in the mapping's order, inside the caller's write transaction.

    `batch_id` is `None` for a **History** edit, which is what keeps it out of a **Batch**'s entries. Every
    entry shares `at`, stored as an ISO 8601 UTC string (invariant 5); a naive moment is refused.
    """
    if at.tzinfo is None:
        raise ValueError("a Decision log timestamp must be UTC-aware")
    recorded_at = at.astimezone(dt.UTC).isoformat()
    write.executemany(
        "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, ?, ?, ?)",
        [(wallpaper_id, batch_id, verdict.value, recorded_at) for wallpaper_id, verdict in verdicts.items()],
    )


def resolve(connection: sqlite3.Connection, wallpaper_ids: Sequence[str]) -> dict[str, ResolvedVerdict]:
    """**Verdict resolution** for many **Wallpapers** at once, in one query, keyed by every id asked about.

    No entries, a legacy **Clearance** last, and an unknown id all resolve to no **Verdict** worth zero.
    """
    requested = list(dict.fromkeys(wallpaper_ids))
    resolved = dict.fromkeys(requested, _ABSENT)
    if not requested:
        return resolved
    # SQLite's parameter limit is 32,766 here, well above any **Pool** this is handed.
    placeholders = ",".join("?" * len(requested))
    query = _resolution_query(
        "SELECT wallpaper_id, resolved FROM resolution",
        restriction=f"WHERE wallpaper_id IN ({placeholders})",
    )
    for row in connection.execute(query, requested).fetchall():
        resolved[str(row["wallpaper_id"])] = _resolved_from(row["resolved"])
    return resolved


def history(connection: sqlite3.Connection, verdict: Verdict | None = None, page: int = 1) -> History:
    """One page of **History**, newest activity first by sequence, optionally narrowed to one resolved
    **Verdict** in SQL.

    `page` is clamped rather than refused, so a stale link shows the last page.
    """
    parameters: list[str] = [] if verdict is None else [verdict.value]
    filtered = verdict is not None
    total = int(connection.execute(_history_count_query(filtered=filtered), parameters).fetchone()[0])
    pages = max(1, -(-total // HISTORY_PAGE_SIZE))
    wanted = min(max(page, 1), pages)
    rows = connection.execute(
        _history_query(filtered=filtered),
        [*parameters, HISTORY_PAGE_SIZE, (wanted - 1) * HISTORY_PAGE_SIZE],
    ).fetchall()
    return History(entries=tuple(map(_history_entry_from, rows)), page=wanted, pages=pages, total=total)


def history_entry(connection: sqlite3.Connection, wallpaper_id: str) -> HistoryEntry | None:
    """One **Wallpaper**'s line in **History**, as the page shows it, or `None` if it has no entry."""
    row = connection.execute(_HISTORY_ENTRY, (wallpaper_id,)).fetchone()
    return None if row is None else _history_entry_from(row)


def favourites(connection: sqlite3.Connection) -> list[str]:
    """Every **Wallpaper** whose resolved **Verdict** is **Favourite**, by id so a seeded draw repeats."""
    query = _resolution_query("SELECT wallpaper_id FROM resolution WHERE resolved = ? ORDER BY wallpaper_id")
    return [str(row["wallpaper_id"]) for row in connection.execute(query, (Verdict.FAVOURITE.value,))]


def explicitly_decided(connection: sqlite3.Connection) -> set[str]:
    """Every **Wallpaper** with an **Explicit Verdict** standing. The only spelling of "explicit"."""
    return {str(row["wallpaper_id"]) for row in connection.execute(_EXPLICITLY_DECIDED)}


def entries(
    connection: sqlite3.Connection, *, batch_id: str | None = None, wallpaper_id: str | None = None
) -> list[DecisionEntry]:
    """The raw entries in sequence order, optionally for one **Batch** or **Wallpaper**."""
    conditions: list[str] = []
    parameters: list[str] = []
    if batch_id is not None:
        conditions.append("batch_id = ?")
        parameters.append(batch_id)
    if wallpaper_id is not None:
        conditions.append("wallpaper_id = ?")
        parameters.append(wallpaper_id)
    query = "SELECT seq, wallpaper_id, batch_id, verdict, recorded_at FROM decision_log"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    rows = connection.execute(f"{query} ORDER BY seq", parameters).fetchall()
    return [
        DecisionEntry(
            seq=int(row["seq"]),
            wallpaper_id=str(row["wallpaper_id"]),
            batch_id=None if row["batch_id"] is None else str(row["batch_id"]),
            entry=_entry_from(row["verdict"]),
            recorded_at=dt.datetime.fromisoformat(str(row["recorded_at"])),
        )
        for row in rows
    ]


_ABSENT = ResolvedVerdict(verdict=None, value=0)
"""A **Wallpaper** with nothing standing: no entries, or a legacy **Clearance** last."""

_EXPLICIT_VALUES = {Verdict.FAVOURITE: 100, Verdict.LIKE: 50, Verdict.BAN: -100}
"""What each **Explicit Verdict** resolves to. An **Ignore** is not here — it is `_IGNORE_VALUE`."""

_IGNORE_VALUE = -10
"""What a resolved **Ignore** is worth. Once, never stacked."""


def _entry_from(stored: object) -> Verdict | Clearance:
    """One `decision_log.verdict` cell as the entry it is: the column also holds legacy **Clearances**."""
    text = str(stored)
    return Clearance.CLEARED if text == Clearance.CLEARED.value else Verdict(text)


def _resolved_from(resolved: object) -> ResolvedVerdict:
    """What a resolved **Verdict** is worth. The rule that *chose* it is `_RESOLUTION_CTE`, never copied
    here.
    """
    if resolved is None:
        return _ABSENT
    verdict = Verdict(str(resolved))
    if verdict is Verdict.IGNORE:
        return ResolvedVerdict(verdict=verdict, value=_IGNORE_VALUE)
    return ResolvedVerdict(verdict=verdict, value=_EXPLICIT_VALUES[verdict])


_RESOLUTION_CTE = f"""
WITH entries AS (
    SELECT wallpaper_id, MAX(seq) AS latest_seq
    FROM decision_log
    {{restriction}}
    GROUP BY wallpaper_id
),
resolution AS (
    SELECT
        entries.wallpaper_id,
        entries.latest_seq,
        latest.recorded_at AS decided_at,
        CASE WHEN latest.verdict != '{Clearance.CLEARED.value}' THEN latest.verdict END AS resolved
    FROM entries
    JOIN decision_log AS latest ON latest.seq = entries.latest_seq
)
"""
"""**Verdict resolution**, as the prelude to every query that needs it (ADR 0015).

The latest entry decides, by `MAX(seq)` and never `recorded_at`: one submission's entries share a timestamp. A
legacy **Clearance** as the latest entry resolves to `NULL`. In SQL because **History** filters and pages over
the resolved **Verdict**.

`{{restriction}}` is a `WHERE` built here, never caller text; values are bound as parameters.
"""


def _history_entry_from(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        wallpaper_id=str(row["wallpaper_id"]),
        resolved=_resolved_from(row["resolved"]),
        decided_at=dt.datetime.fromisoformat(str(row["decided_at"])),
    )


def _resolution_query(select: str, *, restriction: str = "") -> str:
    """A query over the resolved **Decision log**: the shared CTE, then whatever is selected from it."""
    return _RESOLUTION_CTE.format(restriction=restriction) + select


def _history_query(*, filtered: bool) -> str:
    """The **History** page, by `latest_seq` and never `decided_at`, which a whole **Batch** shares."""
    where = "WHERE resolved = ?" if filtered else ""
    return _resolution_query(
        f"SELECT wallpaper_id, resolved, decided_at FROM resolution {where}\n"
        "ORDER BY latest_seq DESC\nLIMIT ? OFFSET ?"
    )


def _history_count_query(*, filtered: bool) -> str:
    """How many lines the same filter matches, for the paging."""
    where = "WHERE resolved = ?" if filtered else ""
    return _resolution_query(f"SELECT COUNT(*) FROM resolution {where}")


_HISTORY_ENTRY = _resolution_query(
    "SELECT wallpaper_id, resolved, decided_at FROM resolution", restriction="WHERE wallpaper_id = ?"
)

_EXPLICITLY_DECIDED = _resolution_query(
    f"SELECT wallpaper_id FROM resolution WHERE resolved IS NOT NULL AND resolved != '{Verdict.IGNORE.value}'"
)
"""Every **Wallpaper** with an **Explicit Verdict** standing: never evicted, and pre-marked when reshown."""
