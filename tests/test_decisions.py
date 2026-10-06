"""The **Decision log**, through `decisions` alone: a real in-memory database, no `compose`.

Entries are appended the way submit and the **History** edit append them. The legacy **Clearance** is the one
row written behind the seam, because `append` takes a **Verdict** and nothing may write a **Clearance** now.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path

import pytest

from tests.conftest import SOURCE
from wallpapi import decisions, storage
from wallpapi.decisions import HISTORY_PAGE_SIZE, HistoryEntry, HistoryRefused, ResolvedVerdict
from wallpapi.model import Clearance, Verdict

AT = dt.datetime(2026, 9, 24, 11, 30, 0, tzinfo=dt.UTC)
LATER = AT + dt.timedelta(minutes=5)
SUBJECT = "wp0000"
WALLPAPERS = tuple(f"wp{n:04d}" for n in range(HISTORY_PAGE_SIZE + 1))
EACH_VERDICT = {
    "wp0000": Verdict.FAVOURITE,
    "wp0001": Verdict.LIKE,
    "wp0002": Verdict.BAN,
    "wp0003": Verdict.IGNORE,
}


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    """A migrated database holding the `wallpapers` rows the log's foreign key wants, and nothing decided."""
    with closing(storage.connect(":memory:")) as connection:
        _migrated_with_wallpapers(connection)
        yield connection


def _migrated_with_wallpapers(connection: sqlite3.Connection) -> None:
    storage.migrate(connection)
    with storage.write(connection) as write:
        write.executemany(
            "INSERT OR IGNORE INTO wallpapers "
            "VALUES (?, 3840, 2160, '1.78', 'general', 'sfw', 1, '', '', '', '')",
            [(w,) for w in WALLPAPERS],
        )


def append(
    connection: sqlite3.Connection,
    verdicts: Mapping[str, Verdict],
    *,
    batch_id: str | None = None,
    at: dt.datetime = AT,
) -> None:
    with storage.write(connection) as write:
        if batch_id is not None:
            write.execute(
                "INSERT INTO batches (id, created_at, size) VALUES (?, ?, ?)",
                (batch_id, at.isoformat(), len(verdicts)),
            )
        decisions.append(write, verdicts, batch_id=batch_id, at=at)


def edit(
    connection: sqlite3.Connection, wallpaper_id: str, verdict: Verdict
) -> HistoryEntry | HistoryRefused:
    """A **History** edit in its own write transaction, as the workflow makes it."""
    with storage.write(connection) as write:
        return decisions.edit(write, wallpaper_id, verdict, at=AT)


def append_legacy_clearance(connection: sqlite3.Connection, wallpaper_id: str) -> None:
    """What a database from before ADR 0015 may hold. Behind the seam: `append` cannot write one."""
    connection.execute(
        "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, NULL, ?, ?)",
        (wallpaper_id, Clearance.CLEARED.value, AT.isoformat()),
    )


def resolved(connection: sqlite3.Connection, wallpaper_id: str) -> ResolvedVerdict:
    return decisions.resolve(connection, [wallpaper_id])[wallpaper_id]


# -- resolution --------------------------------------------------------------------------------------------


def test_no_other_source_file_names_the_table() -> None:
    """`storage` creates it and indexes it in migrations that are history; every query is in `decisions`."""
    naming = sorted(
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*.py")
        if "decision_log" in path.read_text(encoding="utf-8")
    )

    assert naming == ["decisions.py", "storage.py"]


def test_every_id_asked_about_is_answered_and_one_with_no_entries_is_worth_zero(
    connection: sqlite3.Connection,
) -> None:
    """Keyed whatever the log says, because **Scores** sum these across the whole **Pool**."""
    assert decisions.resolve(connection, ["wp0001", "never-seen", "wp0001"]) == {
        "wp0001": ResolvedVerdict(verdict=None, value=0),
        "never-seen": ResolvedVerdict(verdict=None, value=0),
    }
    assert decisions.resolve(connection, []) == {}


def test_each_verdict_resolves_to_its_own_value(connection: sqlite3.Connection) -> None:
    """The spec's numbers as literals, so this disagrees with the implementation if it drifts."""
    append(connection, EACH_VERDICT)

    assert decisions.resolve(connection, ["wp0000", "wp0001", "wp0002", "wp0003"]) == {
        "wp0000": ResolvedVerdict(verdict=Verdict.FAVOURITE, value=100),
        "wp0001": ResolvedVerdict(verdict=Verdict.LIKE, value=50),
        "wp0002": ResolvedVerdict(verdict=Verdict.BAN, value=-100),
        "wp0003": ResolvedVerdict(verdict=Verdict.IGNORE, value=-10),
    }


def test_two_entries_sharing_a_timestamp_resolve_by_sequence(connection: sqlite3.Connection) -> None:
    """Invariant 4. The later entry decides, though nothing in `recorded_at` says which is later."""
    append(connection, {SUBJECT: Verdict.LIKE})
    append(connection, {SUBJECT: Verdict.BAN})
    append(connection, {SUBJECT: Verdict.FAVOURITE})

    logged = decisions.entries(connection, wallpaper_id=SUBJECT)
    assert len({entry.recorded_at for entry in logged}) == 1
    assert resolved(connection, SUBJECT).verdict is Verdict.FAVOURITE


def test_an_ignore_after_a_favourite_resolves_to_ignore(connection: sqlite3.Connection) -> None:
    """The latest entry decides, whatever it is (ADR 0015), and an **Ignore** counts once."""
    append(connection, {SUBJECT: Verdict.FAVOURITE})
    append(connection, {SUBJECT: Verdict.IGNORE}, at=LATER)
    append(connection, {SUBJECT: Verdict.IGNORE}, at=LATER)

    assert resolved(connection, SUBJECT) == ResolvedVerdict(verdict=Verdict.IGNORE, value=-10)


def test_a_legacy_clearance_resolves_to_nothing_and_anything_after_it_decides(
    connection: sqlite3.Connection,
) -> None:
    cleared, judged_again = "wp0000", "wp0001"
    append(connection, {cleared: Verdict.FAVOURITE, judged_again: Verdict.BAN})
    append_legacy_clearance(connection, cleared)
    append_legacy_clearance(connection, judged_again)
    append(connection, {judged_again: Verdict.LIKE})

    assert resolved(connection, cleared) == ResolvedVerdict(verdict=None, value=0)
    assert resolved(connection, judged_again) == ResolvedVerdict(verdict=Verdict.LIKE, value=50)


def test_only_a_standing_explicit_verdict_is_explicitly_decided(connection: sqlite3.Connection) -> None:
    """The SQL spelling of "is an **Explicit Verdict**", over every **Verdict** and the legacy value."""
    append(connection, EACH_VERDICT)
    append(connection, {"wp0004": Verdict.LIKE})
    append_legacy_clearance(connection, "wp0004")
    append(connection, {"wp0005": Verdict.FAVOURITE})
    append(connection, {"wp0005": Verdict.IGNORE})

    assert decisions.explicitly_decided(connection) == {"wp0000", "wp0001", "wp0002"}


def test_favourites_are_what_resolves_to_favourite_now(connection: sqlite3.Connection) -> None:
    """Not what was ever **Favourited**: any later entry, a **Clearance** included, takes it away."""
    append(connection, {"wp0003": Verdict.FAVOURITE, "wp0001": Verdict.FAVOURITE, "wp0002": Verdict.LIKE})
    append(connection, {"wp0004": Verdict.FAVOURITE, "wp0005": Verdict.FAVOURITE, "wp0006": Verdict.BAN})
    append(connection, {"wp0004": Verdict.IGNORE, "wp0006": Verdict.FAVOURITE})
    append_legacy_clearance(connection, "wp0005")

    assert decisions.favourites(connection) == ["wp0001", "wp0003", "wp0006"], "by id, for a seeded draw"


def test_the_lookalike_subjects_are_what_resolves_to_favourite_or_like_now(
    connection: sqlite3.Connection,
) -> None:
    """A **Like** stands beside a **Favourite** as a subject; a later **Ban**, **Ignore** or **Clearance**
    takes either away, and a **Ban** turned **Like** brings one in."""
    append(connection, EACH_VERDICT)
    append(connection, {"wp0006": Verdict.LIKE, "wp0005": Verdict.FAVOURITE, "wp0004": Verdict.BAN})
    append(connection, {"wp0006": Verdict.BAN, "wp0004": Verdict.LIKE, "wp0007": Verdict.LIKE})
    append_legacy_clearance(connection, "wp0007")

    assert decisions.lookalike_subjects(connection) == ["wp0000", "wp0001", "wp0004", "wp0005"], "by id"


def test_mentioned_is_every_wallpaper_with_any_entry_at_all(connection: sqlite3.Connection) -> None:
    """What the **Pool** refuses (ADR 0016): an **Ignore** and a legacy **Clearance** are mentions too."""
    append(connection, {"wp0000": Verdict.IGNORE})
    append_legacy_clearance(connection, "wp0001")
    append(connection, {"wp0002": Verdict.LIKE})
    append(connection, {"wp0002": Verdict.BAN})

    mentioned = connection.execute(decisions.MENTIONED).fetchall()

    assert sorted(str(row[0]) for row in mentioned) == ["wp0000", "wp0001", "wp0002", "wp0002"]


# -- appending -------------------------------------------------------------------------------------------


def test_appending_keeps_the_given_order_and_batch(connection: sqlite3.Connection) -> None:
    append(connection, {"wp0002": Verdict.LIKE, "wp0000": Verdict.IGNORE}, batch_id="b1")
    append(connection, {"wp0000": Verdict.BAN}, at=LATER)

    logged = decisions.entries(connection)

    assert [(e.wallpaper_id, e.batch_id, e.entry, e.recorded_at) for e in logged] == [
        ("wp0002", "b1", Verdict.LIKE, AT),
        ("wp0000", "b1", Verdict.IGNORE, AT),
        ("wp0000", None, Verdict.BAN, LATER),
    ]
    assert [e.seq for e in logged] == sorted(e.seq for e in logged)
    assert [e.entry for e in decisions.entries(connection, batch_id="b1", wallpaper_id="wp0000")] == [
        Verdict.IGNORE
    ], "a History edit is not one of the Batch's entries"


def test_an_append_is_part_of_the_callers_transaction(connection: sqlite3.Connection) -> None:
    """Submit appends and retires the **Pool** rows in one transaction; a failure takes both back."""
    with pytest.raises(RuntimeError), storage.write(connection) as write:
        decisions.append(write, {SUBJECT: Verdict.LIKE}, batch_id=None, at=AT)
        raise RuntimeError

    assert decisions.entries(connection) == []


def test_a_second_ignore_is_appended_not_folded_into_the_first(connection: sqlite3.Connection) -> None:
    """Append-only: an entry the same as the latest is a second entry, not a no-op. **History** is the only
    way to decide a submitted **Wallpaper** again."""
    append(connection, {SUBJECT: Verdict.IGNORE}, batch_id="b1")

    assert isinstance(edit(connection, SUBJECT, Verdict.IGNORE), HistoryEntry)

    assert [e.entry for e in decisions.entries(connection, wallpaper_id=SUBJECT)] == [
        Verdict.IGNORE,
        Verdict.IGNORE,
    ]


def test_the_decision_log_survives_a_restart(tmp_path: Path) -> None:
    """A second connection over the same file, migrated again: nothing appended is lost or rewritten."""
    path = tmp_path / "wallpapi.db"
    with closing(storage.connect(path)) as first:
        _migrated_with_wallpapers(first)
        append(first, EACH_VERDICT, batch_id="b1")
        recorded = decisions.entries(first)

    with closing(storage.connect(path)) as second:
        _migrated_with_wallpapers(second)
        restored = decisions.entries(second)

    assert len(restored) == len(EACH_VERDICT)
    assert restored == recorded


def test_a_legacy_clearance_is_read_back_as_one(connection: sqlite3.Connection) -> None:
    append_legacy_clearance(connection, SUBJECT)

    assert [e.entry for e in decisions.entries(connection)] == [Clearance.CLEARED]


def test_timestamps_are_stored_as_iso_8601_utc_strings(connection: sqlite3.Connection) -> None:
    """Invariant 5: text the caller can read without an adapter, in UTC whatever zone the moment came in."""
    in_paris = AT.astimezone(dt.timezone(dt.timedelta(hours=2)))
    append(connection, {SUBJECT: Verdict.LIKE}, at=in_paris)

    stored = connection.execute("SELECT recorded_at, typeof(recorded_at) FROM decision_log").fetchone()

    assert tuple(stored) == ("2026-09-24T11:30:00+00:00", "text")
    assert decisions.entries(connection)[0].recorded_at == AT


def test_a_moment_with_no_zone_is_refused(connection: sqlite3.Connection) -> None:
    """A naive moment could be any zone; the injected clock is always UTC-aware."""
    with pytest.raises(ValueError, match="UTC"), storage.write(connection) as write:
        decisions.append(write, {SUBJECT: Verdict.LIKE}, batch_id=None, at=AT.replace(tzinfo=None))

    assert decisions.entries(connection) == []


# -- history ---------------------------------------------------------------------------------------------


def test_history_is_one_row_per_wallpaper_newest_activity_first_by_sequence(
    connection: sqlite3.Connection,
) -> None:
    """Every entry shares a timestamp, so only the sequence can order them. A legacy **Clearance** has a
    row, resolved to nothing."""
    append(connection, {"wp0000": Verdict.LIKE, "wp0001": Verdict.BAN, "wp0002": Verdict.FAVOURITE})
    append(connection, {"wp0001": Verdict.LIKE})
    append_legacy_clearance(connection, "wp0002")

    listing = decisions.history(connection)

    assert [(e.wallpaper_id, e.resolved.verdict) for e in listing.entries] == [
        ("wp0002", None),
        ("wp0001", Verdict.LIKE),
        ("wp0000", Verdict.LIKE),
    ]
    assert (listing.total, listing.page, listing.pages) == (3, 1, 1)


def test_history_shows_the_deciding_entrys_timestamp(connection: sqlite3.Connection) -> None:
    append(connection, {SUBJECT: Verdict.LIKE})
    append(connection, {SUBJECT: Verdict.BAN}, at=LATER)

    (entry,) = decisions.history(connection).entries

    assert entry.decided_at == LATER


def test_the_filter_narrows_to_one_resolved_verdict_and_the_count_matches_it(
    connection: sqlite3.Connection,
) -> None:
    """By what each **Wallpaper** resolves to now: the **Banned** one was **Liked** first."""
    append(connection, {"wp0000": Verdict.LIKE, "wp0001": Verdict.LIKE, "wp0002": Verdict.FAVOURITE})
    append(connection, {"wp0001": Verdict.BAN, "wp0002": Verdict.IGNORE, "wp0003": Verdict.IGNORE})

    def listed(verdict: Verdict) -> tuple[list[str], int]:
        listing = decisions.history(connection, verdict)
        return [e.wallpaper_id for e in listing.entries], listing.total

    assert listed(Verdict.LIKE) == (["wp0000"], 1)
    assert listed(Verdict.BAN) == (["wp0001"], 1)
    assert listed(Verdict.FAVOURITE) == ([], 0)
    assert listed(Verdict.IGNORE) == (["wp0003", "wp0002"], 2)
    assert decisions.history(connection).total == 4


def test_history_pages_by_a_hundred_and_clamps_a_page_past_either_end(connection: sqlite3.Connection) -> None:
    for wallpaper_id in WALLPAPERS:
        append(connection, {wallpaper_id: Verdict.LIKE})

    first = decisions.history(connection)
    second = decisions.history(connection, page=2)

    assert (first.total, first.pages, len(first.entries)) == (HISTORY_PAGE_SIZE + 1, 2, HISTORY_PAGE_SIZE)
    assert first.entries[0].wallpaper_id == WALLPAPERS[-1]
    assert [e.wallpaper_id for e in second.entries] == [WALLPAPERS[0]]
    assert decisions.history(connection, page=99).page == 2
    assert decisions.history(connection, page=0).page == 1


def test_a_page_knows_its_neighbours(connection: sqlite3.Connection) -> None:
    for wallpaper_id in WALLPAPERS:
        append(connection, {wallpaper_id: Verdict.LIKE})

    first = decisions.history(connection)
    second = decisions.history(connection, page=2)

    assert (first.previous_page, first.next_page) == (None, 2)
    assert (second.previous_page, second.next_page) == (1, None)


def test_one_wallpapers_line_is_the_line_the_page_shows(connection: sqlite3.Connection) -> None:
    """What a **History** edit swaps back in, resolved and dated as the whole page would show it."""
    append(connection, {SUBJECT: Verdict.LIKE, "wp0001": Verdict.BAN})
    append(connection, {SUBJECT: Verdict.FAVOURITE}, at=LATER)

    assert decisions.history_entry(connection, SUBJECT) == decisions.history(connection).entries[0]
    assert decisions.history_entry(connection, "wp0002") is None


def test_an_empty_log_has_one_empty_page(connection: sqlite3.Connection) -> None:
    listing = decisions.history(connection, Verdict.LIKE)

    assert (listing.entries, listing.total, listing.page, listing.pages) == ((), 0, 1, 1)


def test_the_history_filter_offers_every_verdict_once_with_ignore_last() -> None:
    """**Ignore**, the commonest by thousands a week, goes last, after the ones worth looking for."""
    assert sorted(decisions.HISTORY_FILTERS) == sorted(Verdict)
    assert decisions.HISTORY_FILTERS[-1] is Verdict.IGNORE
    assert decisions.HISTORY_FILTERS[:2] == (Verdict.FAVOURITE, Verdict.LIKE)
