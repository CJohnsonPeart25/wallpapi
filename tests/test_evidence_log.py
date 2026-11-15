"""The **Evidence log**, through `evidence_log` alone: a real in-memory database, no `compose`.

**Judgements** are appended as the grid and **History** append them, and built by hand where a test needs a
shape nothing writes yet: a **Skip**, a converted (version 0) **Judgement**, a **Ranking stamp**. The
example-case test is the one that writes rows behind the seam, because its **Judgement** ids are the
cases' own and a stored id is otherwise always the hash of its content.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from tests.conftest import committing_as
from tests.fakes import FakeClock
from wallpapi import evidence_log, storage
from wallpapi.evidence_log import HISTORY_PAGE_SIZE, HistoryEntry, HistoryRefused, ResolvedVerdict
from wallpapi.model import Verdict, Zone
from wallpapi.taste.gauntlet import GAUNTLET_V1, GauntletResponse, GauntletTile
from wallpapi.taste.judgement import (
    Instrument,
    Judgement,
    LogPosition,
    Provenance,
    RankingStamp,
    Shown,
    instant,
)
from wallpapi.taste.order import ORDER_V1, OrderTile
from wallpapi.taste.pick import PICK_V1, PickResponse, PickTile
from wallpapi.taste.reductions import evidence
from wallpapi.taste.resolution import EXAMPLE_CASES, ExampleCase, resolve
from wallpapi.taste.verdict_grid import (
    VERDICT_GRID_V0,
    VERDICT_GRID_V1,
    GridResponse,
    LegacyEntry,
    LegacyTileResponse,
    TileResponse,
)
from wallpapi.taste.verdict_grid import Verdict as GridVerdict

AT = dt.datetime(2026, 10, 27, 9, 30, 0, tzinfo=dt.UTC)
LATER = AT + dt.timedelta(minutes=5)
SUBJECT = "wp0000"
WALLPAPERS = tuple(f"wp{n:04d}" for n in range(HISTORY_PAGE_SIZE + 1))
CASE_WALLPAPERS = ("aaaaaa", "bbbbbb")
"""The **Wallpapers** `taste.resolution.EXAMPLE_CASES` are about, by bare id."""


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    """A migrated database holding the `wallpapers` rows the log's trigger wants, and nothing judged."""
    with closing(storage.connect(":memory:")) as connection:
        storage.migrate(connection)
        _know_every_wallpaper(connection)
        yield connection


def _know_every_wallpaper(connection: sqlite3.Connection) -> None:
    """The `wallpapers` rows the log's trigger wants."""
    with storage.write(connection) as write:
        write.executemany(
            "INSERT INTO wallpapers VALUES (?, 3840, 2160, '1.78', 'general', 'sfw', 1, '', '', '', '')",
            [(w,) for w in (*WALLPAPERS, *CASE_WALLPAPERS)],
        )


def key(wallpaper_id: str) -> str:
    return f"wallhaven:{wallpaper_id}"


def grid(
    *tiles: tuple[str, TileResponse],
    at: dt.datetime = AT,
    submitted: bool = True,
    screen: str = "batch",
    **recorded: object,
) -> Judgement:
    """A version 1 grid **Judgement** of these tiles, in order."""
    return Judgement(
        instrument=VERDICT_GRID_V1,
        screen=screen,
        shown=tuple(Shown(key(w), response) for w, response in tiles),
        recorded_at=at,
        app="wallpapi",
        app_version="0.1.0",
        response=GridResponse(submitted=submitted),
        **recorded,  # pyright: ignore[reportArgumentType]
    )


def converted(*entries: tuple[str, LegacyEntry], at: dt.datetime = AT, screen: str = "history") -> Judgement:
    """A version 0 **Judgement**, as migration 12 converts a **Decision log** entry or **Batch**."""
    return Judgement(
        instrument=VERDICT_GRID_V0,
        screen=screen,
        shown=tuple(Shown(key(w), LegacyTileResponse(entry)) for w, entry in entries),
        recorded_at=at,
        app="wallpapi",
        app_version="0.1.0",
    )


def pick(
    *tiles: tuple[str, PickTile],
    at: dt.datetime = AT,
    none_of_these: bool = False,
    provenance: Provenance | None = None,
    **recorded: object,
) -> Judgement:
    """A **Pick** of these tiles on the compare **Screen**, in order."""
    return Judgement(
        instrument=PICK_V1,
        screen="compare",
        shown=tuple(Shown(key(w), response, provenance or {}) for w, response in tiles),
        recorded_at=at,
        app="wallpapi",
        app_version="0.1.0",
        response=PickResponse(none_of_these=none_of_these),
        **recorded,  # pyright: ignore[reportArgumentType]
    )


def order(
    *tiles: tuple[str, OrderTile],
    at: dt.datetime = AT,
    provenance: Provenance | None = None,
    **recorded: object,
) -> Judgement:
    """An **Order** of these tiles on the compare **Screen**, in order."""
    return Judgement(
        instrument=ORDER_V1,
        screen="compare",
        shown=tuple(Shown(key(w), response, provenance or {}) for w, response in tiles),
        recorded_at=at,
        app="wallpapi",
        app_version="0.1.0",
        **recorded,  # pyright: ignore[reportArgumentType]
    )


def gauntlet(
    *tiles: tuple[str, GauntletTile],
    at: dt.datetime = AT,
    streak: int = 0,
    none_of_these: bool = False,
    provenance: Provenance | None = None,
    **recorded: object,
) -> Judgement:
    """A **Gauntlet** screen of these tiles on the compare **Screen**, in order, its champion having carried
    `streak` onto it."""
    return Judgement(
        instrument=GAUNTLET_V1,
        screen="compare",
        shown=tuple(Shown(key(w), response, provenance or {}) for w, response in tiles),
        recorded_at=at,
        app="wallpapi",
        app_version="0.1.0",
        response=GauntletResponse(streak=streak, none_of_these=none_of_these),
        **recorded,  # pyright: ignore[reportArgumentType]
    )


def append(connection: sqlite3.Connection, *judgements: Judgement) -> None:
    for judgement in judgements:
        with storage.write(connection) as write:
            evidence_log.append(write, judgement)


def edit(
    connection: sqlite3.Connection, wallpaper_id: str, verdict: Verdict, clock: FakeClock | None = None
) -> HistoryEntry | HistoryRefused:
    """A **History** edit in its own write transaction, as the workflow makes it."""
    with storage.write(connection) as write:
        return evidence_log.edit(write, wallpaper_id, verdict, clock=clock or FakeClock(AT))


def resolved(connection: sqlite3.Connection, wallpaper_id: str) -> ResolvedVerdict:
    return evidence_log.resolve(connection, [wallpaper_id])[wallpaper_id]


BLANK = TileResponse()
LIKED = TileResponse(verdict=GridVerdict.LIKE)
BANNED = TileResponse(verdict=GridVerdict.BAN)
STARRED = TileResponse(favourite=True)
PICKED = PickTile(picked=True)
PASSED = PickTile()
UNRANKED = OrderTile()
WON = GauntletTile(picked=True)
LOST = GauntletTile()


def ranked(rank: int, *, favourite: bool = False) -> OrderTile:
    return OrderTile(rank=rank, favourite=favourite)


# -- appending and reading back ----------------------------------------------------------------------------


def test_a_judgement_written_then_read_back_is_the_same_and_earns_the_id_it_was_stored_under(
    connection: sqlite3.Connection,
) -> None:
    """Every field kept, those outside the id included: **Provenance**, the **Ranking stamp**, the context,
    the offset and the first-shown time. Read back, it is rebuilt from its rows and hashed again."""
    stamp = RankingStamp(
        read=LogPosition(recorded_at=AT - dt.timedelta(hours=1), judgement_id="f" * 64),
        space="clip",
        radius=0.1,
        decay=2.5,
    )
    judgement = Judgement(
        instrument=VERDICT_GRID_V1,
        screen="batch",
        shown=(
            Shown(key("wp0002"), STARRED, {"zone": "banger", "mix": "Explore"}),
            Shown(key("wp0000"), TileResponse(preview_opened=True), {"zone": "unknown"}),
            Shown(key("wp0001"), BANNED),
        ),
        recorded_at=AT.replace(microsecond=0),
        app="wallpapi",
        app_version="0.1.0",
        response=GridResponse(submitted=True),
        utc_offset=dt.timedelta(hours=1),
        first_shown_at=AT - dt.timedelta(seconds=40),
        ranking=stamp,
        context={"theme": "dark", "a name this version does not know": 3},
    )

    append(connection, judgement)
    (read,) = evidence_log.judgements(connection)

    assert read == judgement
    assert read.id == judgement.id


def test_a_converted_judgement_and_a_skip_are_read_back_as_written(connection: sqlite3.Connection) -> None:
    """Version 0 keeps the old words, a **Clearance** among them; a **Skip** keeps its blank screen."""
    old = converted(("wp0000", LegacyEntry.CLEARED), ("wp0001", LegacyEntry.FAVOURITE), screen="batch")
    skip = grid(("wp0002", BLANK), ("wp0003", LIKED), at=LATER, submitted=False)

    append(connection, old, skip)

    assert evidence_log.judgements(connection) == [old, skip]


def test_a_pick_and_a_none_of_these_are_read_back_as_written(connection: sqlite3.Connection) -> None:
    """**Pick** keeps its tiles' winner and star and its screen's "none of these", with everything a screen
    drawn from a **Mix** records about itself."""
    chosen = pick(
        ("wp0000", PASSED),
        ("wp0001", PICKED),
        ("wp0002", PickTile(favourite=True)),
        provenance={"zone": "unknown", "slot_zone": "banger", "mix": "explore"},
        first_shown_at=AT - dt.timedelta(seconds=3),
        ranking=RankingStamp(read=None, space="fallback", radius=0.1, decay=4.0),
        utc_offset=dt.timedelta(hours=-5),
        context={"theme": "light"},
    )
    none = pick(("wp0003", PASSED), ("wp0000", PASSED), at=LATER, none_of_these=True)

    append(connection, chosen, none)

    assert evidence_log.judgements(connection) == [chosen, none]


def test_a_gauntlet_is_read_back_as_written_streak_and_champion_included(
    connection: sqlite3.Connection,
) -> None:
    """**Gauntlet** keeps its tiles' winner and star, its screen's streak and "none of these", and the
    champion's **Provenance** saying it was kept rather than drawn, with everything a screen drawn from a
    **Mix** records about itself."""
    drawn = {"zone": "unknown", "slot_zone": "unknown", "mix": "explore"}
    defended = Judgement(
        instrument=GAUNTLET_V1,
        screen="compare",
        shown=(
            Shown(key("wp0000"), LOST, drawn),
            Shown(key("wp0001"), WON, {**drawn, "zone": "banger", "kept": "champion"}),
            Shown(key("wp0002"), GauntletTile(favourite=True), drawn),
        ),
        recorded_at=AT,
        app="wallpapi",
        app_version="0.1.0",
        response=GauntletResponse(streak=2),
        first_shown_at=AT - dt.timedelta(seconds=2),
        ranking=RankingStamp(read=None, space="fallback", radius=0.1, decay=4.0),
        utc_offset=dt.timedelta(hours=1),
        context={"theme": "dark"},
    )
    first = gauntlet(("wp0003", WON), ("wp0000", LOST), at=LATER)
    dismissed = gauntlet(
        ("wp0003", LOST), ("wp0004", LOST), at=LATER + dt.timedelta(seconds=1), streak=1, none_of_these=True
    )

    append(connection, defended, first, dismissed)

    assert evidence_log.judgements(connection) == [defended, first, dismissed]


def test_a_tile_kept_as_a_gauntlets_champion_says_so_beside_where_it_was_first_drawn() -> None:
    """The one **Provenance** label a drawn tile never has."""
    drawn = evidence_log.provenance(zone=Zone.UNKNOWN, slot_zone=Zone.BANGER, mix="explore")
    kept = evidence_log.provenance(zone=Zone.UNKNOWN, slot_zone=Zone.BANGER, mix="explore", champion=True)

    assert drawn == {"zone": "unknown", "slot_zone": "banger", "mix": "explore"}
    assert kept == {**drawn, "kept": "champion"}


def test_an_order_partial_complete_or_blank_is_read_back_as_written(connection: sqlite3.Connection) -> None:
    """**Order** keeps each tile's rank, an unranked one's absence of one, and its star, with everything a
    screen drawn from a **Mix** records about itself; it has no screen response to keep."""
    partial = order(
        ("wp0000", UNRANKED),
        ("wp0001", ranked(2)),
        ("wp0002", OrderTile(favourite=True)),
        ("wp0003", ranked(1, favourite=True)),
        provenance={"zone": "banger", "slot_zone": "banger", "mix": "refine"},
        first_shown_at=AT - dt.timedelta(seconds=7),
        ranking=RankingStamp(read=None, space="fallback", radius=0.1, decay=4.0),
        utc_offset=dt.timedelta(hours=1),
        context={"theme": "dark"},
    )
    complete = order(("wp0001", ranked(2)), ("wp0000", ranked(1)), at=LATER)
    blank = order(("wp0002", UNRANKED), ("wp0003", UNRANKED), at=LATER + dt.timedelta(seconds=1))

    append(connection, partial, complete, blank)

    assert evidence_log.judgements(connection) == [partial, complete, blank]


def test_the_log_reads_in_recorded_order_and_on_a_tie_the_lower_id_comes_last(
    connection: sqlite3.Connection,
) -> None:
    """So the last **Judgement** on an **Item** is the one that decides it, as resolution reads the log."""
    first, second = (grid((w, LIKED), at=AT) for w in ("wp0000", "wp0001"))
    lower, higher = sorted((first, second), key=lambda judgement: judgement.id)
    latest = grid(("wp0002", LIKED), at=LATER)

    append(connection, latest, lower, higher)

    assert evidence_log.judgements(connection) == [higher, lower, latest]


def test_the_same_judgement_appended_twice_is_held_once(connection: sqlite3.Connection) -> None:
    """Identified by its content: the same act is the same **Judgement**, which is all a later merge of two
    logs needs."""
    judgement = grid(("wp0000", LIKED))

    append(connection, judgement, judgement)

    assert evidence_log.judgements(connection) == [judgement]


def test_a_judgement_appended_between_the_logs_two_reads_waits_whole_for_the_next(db_path: Path) -> None:
    """Read outside a transaction, the log's two reads see two snapshots, and another tab can append between
    them. Screens first, so that one has tiles and no screen and is left out whole; tiles first, it would
    have a screen and no tiles, which no **Judgement** can be. A caller wanting the log to agree with
    anything else it reads still reads in one transaction."""
    first = grid(("wp0000", LIKED))
    meanwhile = grid(("wp0001", LIKED), at=LATER)
    with closing(storage.connect(db_path)) as reader, closing(storage.connect(db_path)) as other_tab:
        storage.migrate(reader)
        _know_every_wallpaper(reader)
        append(reader, first)

        with committing_as(reader, "FROM judgement", lambda: append(other_tab, meanwhile), nth=2):
            read = evidence_log.judgements(reader)

        assert read == [first]
        assert evidence_log.judgements(reader) == [first, meanwhile]


def test_a_judgement_whose_rows_no_longer_earn_their_id_is_refused_on_reading(
    connection: sqlite3.Connection,
) -> None:
    """A stored id is never trusted: rows edited behind the log (here, with its trigger dropped) are found."""
    append(connection, grid(("wp0000", LIKED)))
    connection.execute("DROP TRIGGER judgement_items_update_refused")
    connection.execute("UPDATE judgement_items SET verdict = 'ban'")

    with pytest.raises(ValueError, match="does not earn"):
        evidence_log.judgements(connection)


@pytest.mark.parametrize("statement", ["UPDATE judgements SET screen = 'x'", "DELETE FROM judgement_items"])
def test_nothing_in_the_log_is_updated_or_deleted(connection: sqlite3.Connection, statement: str) -> None:
    append(connection, grid(("wp0000", LIKED)))

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(statement)


def test_an_append_is_part_of_the_callers_transaction(connection: sqlite3.Connection) -> None:
    """Submit appends and retires the **Pool** rows in one transaction; a failure takes both back."""
    with pytest.raises(RuntimeError), storage.write(connection) as write:
        evidence_log.append(write, grid(("wp0000", LIKED)))
        raise RuntimeError

    assert evidence_log.judgements(connection) == []


def test_a_judgement_naming_an_unknown_wallpaper_is_refused_whole(connection: sqlite3.Connection) -> None:
    """The **Decision log**'s foreign key, kept by a trigger: nothing of the **Judgement** is left behind,
    and the caller's transaction carries on."""
    with storage.write(connection) as write:
        with pytest.raises(sqlite3.IntegrityError):
            evidence_log.append(write, grid(("wp0000", LIKED), ("never-seen", BLANK)))
        evidence_log.append(write, grid(("wp0001", LIKED)))

    assert [j.shown_set for j in evidence_log.judgements(connection)] == [(key("wp0001"),)]


def test_only_wallhaven_items_of_a_kept_instrument_are_appended(connection: sqlite3.Connection) -> None:
    """The log keeps the verdict grid's two versions, **Pick**, **Order** and **Gauntlet**, each **Response**
    in its own shape, and Wallhaven is the only **Item source**."""
    elsewhere = Judgement(
        instrument=VERDICT_GRID_V1,
        screen="batch",
        shown=(Shown("local:photo.jpg", LIKED),),
        recorded_at=AT,
        app="wallpapi",
        app_version="0.1.0",
        response=GridResponse(submitted=True),
    )
    unbuilt = Judgement(
        instrument=Instrument("stream", 1),
        screen="compare",
        shown=(Shown(key("wp0000"), PASSED),),
        recorded_at=AT,
        app="wallpapi",
        app_version="0.1.0",
        response=PickResponse(),
    )
    order_of_pick_tiles = order(("wp0000", PASSED))  # pyright: ignore[reportArgumentType]
    gauntlet_of_pick_tiles = gauntlet(("wp0000", PICKED))  # pyright: ignore[reportArgumentType]
    gauntlet_with_a_pick_screen = Judgement(
        instrument=GAUNTLET_V1,
        screen="compare",
        shown=(Shown(key("wp0000"), WON),),
        recorded_at=AT,
        app="wallpapi",
        app_version="0.1.0",
        response=PickResponse(),
    )
    pick_of_gauntlet_tiles = pick(("wp0000", WON))  # pyright: ignore[reportArgumentType]
    order_with_a_screen_response = order(("wp0000", ranked(1)), response=PickResponse())
    pick_of_grid_tiles = Judgement(
        instrument=PICK_V1,
        screen="compare",
        shown=(Shown(key("wp0000"), LIKED),),
        recorded_at=AT,
        app="wallpapi",
        app_version="0.1.0",
        response=PickResponse(),
    )
    pick_with_a_grid_screen = Judgement(
        instrument=PICK_V1,
        screen="compare",
        shown=(Shown(key("wp0000"), PICKED),),
        recorded_at=AT,
        app="wallpapi",
        app_version="0.1.0",
        response=GridResponse(submitted=True),
    )

    with storage.write(connection) as write:
        with pytest.raises(ValueError, match="source"):
            evidence_log.append(write, elsewhere)
        for refused in (
            unbuilt,
            pick_of_grid_tiles,
            pick_with_a_grid_screen,
            order_of_pick_tiles,
            order_with_a_screen_response,
            gauntlet_of_pick_tiles,
            gauntlet_with_a_pick_screen,
            pick_of_gauntlet_tiles,
        ):
            with pytest.raises(ValueError, match=refused.instrument.name):
                evidence_log.append(write, refused)

    assert evidence_log.judgements(connection) == []


def test_times_are_stored_as_fixed_utc_text_to_the_microsecond(connection: sqlite3.Connection) -> None:
    """`taste.judgement.instant`: six microsecond digits always, so text order is time order."""
    in_paris = AT.astimezone(dt.timezone(dt.timedelta(hours=2)))
    append(connection, grid(("wp0000", LIKED), at=in_paris, first_shown_at=in_paris))

    stored = connection.execute("SELECT recorded_at, first_shown_at FROM judgements").fetchone()

    assert tuple(stored) == ("2026-10-27T09:30:00.000000+00:00", "2026-10-27T09:30:00.000000+00:00")


def test_every_stored_judgement_carries_the_format_version(connection: sqlite3.Connection) -> None:
    append(connection, grid(("wp0000", LIKED)))

    assert connection.execute("SELECT format_version FROM judgements").fetchone()[0] == 1


def test_an_item_key_is_a_wallhaven_id_namespaced_by_its_source() -> None:
    assert evidence_log.item_key("94x38z") == "wallhaven:94x38z"
    assert evidence_log.wallpaper_id("wallhaven:94x38z") == "94x38z"
    with pytest.raises(ValueError, match="source"):
        evidence_log.wallpaper_id("local:94x38z")


# -- resolution --------------------------------------------------------------------------------------------


def _write_case(connection: sqlite3.Connection, case: ExampleCase) -> None:
    """Each **Judgement** of a case as version 0 rows under the case's own id. Behind the seam: an id
    `append` stores is the hash of its content, and the cases' ties turn on how their ids compare."""
    by_judgement: dict[str, list[tuple[str, LegacyEntry, dt.datetime]]] = {}
    for said in case.said:
        by_judgement.setdefault(said.judgement_id, []).append((said.item, said.value, said.recorded_at))
    with storage.write(connection) as write:
        for judgement_id, said in by_judgement.items():
            write.execute(
                "INSERT INTO judgements (id, format_version, instrument, instrument_version, screen,"
                " recorded_at, app, app_version, context) VALUES (?, 1, 'verdict grid', 0, 'batch', ?,"
                " 'wallpapi', '0.1.0', '{}')",
                (judgement_id, instant(said[0][2])),
            )
            write.executemany(
                "INSERT INTO judgement_items (judgement_id, position, item, favourite, preview_opened,"
                " entry, provenance) VALUES (?, ?, ?, 0, 0, ?, '{}')",
                [
                    (judgement_id, position, item, entry.value)
                    for position, (item, entry, _) in enumerate(said)
                ],
            )


@pytest.mark.parametrize("case", EXAMPLE_CASES, ids=[case.name for case in EXAMPLE_CASES])
def test_the_sql_resolves_each_of_tastes_example_cases_to_its_standing(
    connection: sqlite3.Connection, case: ExampleCase
) -> None:
    """Invariant 4's second spelling, held to the first's cases: the deciding **Judgement** and what it said,
    a **Clearance** last standing at nothing."""
    _write_case(connection, case)

    lines = {key(e.wallpaper_id): e for e in evidence_log.history(connection).entries}

    assert {item: line.judgement_id for item, line in lines.items()} == {
        item: standing.judgement_id for item, standing in case.standing.items()
    }
    assert {item: line.resolved.verdict for item, line in lines.items()} == {
        item: None if standing.value is LegacyEntry.CLEARED else Verdict(standing.value.value)
        for item, standing in case.standing.items()
    }


def _standing_in_sql(connection: sqlite3.Connection) -> dict[str, tuple[str, float]]:
    listing = evidence_log.history(connection)
    assert listing.pages == 1
    return {key(e.wallpaper_id): (e.judgement_id, e.resolved.value) for e in listing.entries}


def _standing_in_taste(judgements: list[Judgement]) -> dict[str, tuple[str, float]]:
    return {
        item: (piece.judgement_id, piece.strength) for item, piece in resolve(evidence(judgements)).items()
    }


def test_the_sql_standing_of_every_item_is_tastes_reduction_then_resolution(
    connection: sqlite3.Connection,
) -> None:
    """The second spelling of the version 1 **Reduction**: an unmarked tile on a submitted **Batch** is an
    **Ignore**, a starred one a **Favourite**, a **Skip** says nothing whatever its tiles hold, and a
    **History** edit overturns the **Batch**. Converted **Judgements** among them, a **Clearance** too.

    And of **Pick**'s, **Order**'s and **Gauntlet**'s: a **Pick**, an **Order**, partial, complete or blank,
    a **Gauntlet** screen whatever its streak, or a "none of these" on either, says nothing absolute about
    any tile, so a later one neither overturns a **Verdict** nor gives a tile seen only there a line; a tile
    starred on one is a **Favourite** like any other, ranked, picked or not."""
    batch = grid(
        ("wp0000", BLANK), ("wp0001", STARRED), ("wp0002", LIKED), ("wp0003", BANNED), ("wp0004", BLANK)
    )
    skip = grid(
        ("wp0005", BLANK),
        ("wp0006", LIKED),
        ("wp0000", TileResponse(preview_opened=True)),
        at=LATER,
        submitted=False,
    )
    overturned = grid(("wp0002", TileResponse(verdict=GridVerdict.IGNORE)), at=LATER, screen="history")
    starred_from_history = grid(("wp0003", STARRED), at=LATER, screen="history")
    old = converted(("wp0007", LegacyEntry.FAVOURITE), ("wp0008", LegacyEntry.LIKE), screen="batch")
    cleared = converted(("wp0008", LegacyEntry.CLEARED), at=LATER)
    latest = LATER + dt.timedelta(minutes=5)
    picked_over = pick(("wp0009", PICKED), ("wp0010", PASSED), ("wp0002", PASSED), at=latest)
    passed_over = pick(("wp0011", PASSED), ("wp0000", PASSED), at=latest, none_of_these=True)
    starred_on_a_pick = pick(
        ("wp0012", PickTile(favourite=True)),
        ("wp0004", PICKED),
        ("wp0006", PickTile(favourite=True)),
        at=latest,
    )
    ordered_over = order(("wp0013", ranked(1)), ("wp0001", ranked(2)), ("wp0003", UNRANKED), at=latest)
    blank_order = order(("wp0014", UNRANKED), ("wp0002", UNRANKED), at=latest)
    starred_on_an_order = order(
        ("wp0015", OrderTile(favourite=True)),
        ("wp0000", ranked(1, favourite=True)),
        ("wp0005", ranked(2)),
        at=latest + dt.timedelta(seconds=1),
    )
    defended = gauntlet(("wp0016", WON), ("wp0002", LOST), ("wp0017", LOST), at=latest, streak=3)
    dismissed = gauntlet(("wp0016", LOST), ("wp0018", LOST), at=latest, streak=4, none_of_these=True)
    starred_on_a_gauntlet = gauntlet(
        ("wp0019", GauntletTile(picked=True, favourite=True)),
        ("wp0001", GauntletTile(favourite=True)),
        ("wp0004", LOST),
        at=latest + dt.timedelta(seconds=2),
        streak=1,
    )
    logged = [
        batch,
        skip,
        overturned,
        starred_from_history,
        old,
        cleared,
        picked_over,
        passed_over,
        starred_on_a_pick,
        ordered_over,
        blank_order,
        starred_on_an_order,
        defended,
        dismissed,
        starred_on_a_gauntlet,
    ]

    append(connection, *logged)

    assert _standing_in_sql(connection) == _standing_in_taste(logged)
    assert _standing_in_sql(connection) == {
        key("wp0000"): (starred_on_an_order.id, 100.0),
        key("wp0001"): (starred_on_a_gauntlet.id, 100.0),
        key("wp0002"): (overturned.id, -10.0),
        key("wp0003"): (starred_from_history.id, 100.0),
        key("wp0004"): (batch.id, -10.0),
        key("wp0006"): (starred_on_a_pick.id, 100.0),
        key("wp0007"): (old.id, 100.0),
        key("wp0008"): (cleared.id, 0.0),
        key("wp0012"): (starred_on_a_pick.id, 100.0),
        key("wp0015"): (starred_on_an_order.id, 100.0),
        key("wp0019"): (starred_on_a_gauntlet.id, 100.0),
    }, (
        "worked by hand: no line for wp0005, seen only on the Skip and ranked on an Order, nor wp0009 to "
        "wp0011, only on Picks, nor wp0013 and wp0014, only on Orders, nor wp0016 to wp0018, only on "
        "unstarred Gauntlet tiles; wp0002 and wp0004 keep their Ignores through the Gauntlet"
    )


def test_a_skip_after_a_verdict_leaves_the_verdict_standing(connection: sqlite3.Connection) -> None:
    """A **Skip** is never the latest word on an **Item**: it says nothing at all."""
    batch = grid(("wp0000", LIKED))
    skip = grid(("wp0000", BANNED), at=LATER, submitted=False)
    append(connection, batch, skip)

    assert (
        _standing_in_sql(connection) == _standing_in_taste([batch, skip]) == {key("wp0000"): (batch.id, 50.0)}
    )


def test_every_id_asked_about_is_answered_and_one_with_nothing_standing_is_worth_zero(
    connection: sqlite3.Connection,
) -> None:
    """Keyed whatever the log says, because **Scores** sum these across the whole **Pool**."""
    append(connection, grid(("wp0001", LIKED), submitted=False))

    assert evidence_log.resolve(connection, ["wp0001", "never-seen", "wp0001"]) == {
        "wp0001": ResolvedVerdict(verdict=None, value=0),
        "never-seen": ResolvedVerdict(verdict=None, value=0),
    }
    assert evidence_log.resolve(connection, []) == {}


def test_each_mark_resolves_to_its_own_value(connection: sqlite3.Connection) -> None:
    """The spec's numbers as literals, so this disagrees with the implementation if it drifts."""
    append(connection, grid(("wp0000", STARRED), ("wp0001", LIKED), ("wp0002", BANNED), ("wp0003", BLANK)))

    assert evidence_log.resolve(connection, ["wp0000", "wp0001", "wp0002", "wp0003"]) == {
        "wp0000": ResolvedVerdict(verdict=Verdict.FAVOURITE, value=100),
        "wp0001": ResolvedVerdict(verdict=Verdict.LIKE, value=50),
        "wp0002": ResolvedVerdict(verdict=Verdict.BAN, value=-100),
        "wp0003": ResolvedVerdict(verdict=Verdict.IGNORE, value=-10),
    }


def test_the_decided_are_every_wallpaper_with_a_verdict_or_favourite_ever_recorded(
    connection: sqlite3.Connection,
) -> None:
    """What the **Pool** refuses (ADR 0027): an unmarked tile on a submitted **Batch** and a legacy
    **Clearance** are decisions; an **Item** only ever on a **Skip** is not, whatever was marked on it. Nor
    is one only ever on a **Line-up**, picked, passed over, answered "none of these", ranked or left unranked,
    kept as a **Gauntlet**'s champion or not, unless it was starred there."""
    append(
        connection,
        grid(("wp0000", BLANK), ("wp0001", LIKED)),
        converted(("wp0002", LegacyEntry.CLEARED)),
        grid(("wp0003", BLANK), ("wp0004", STARRED), ("wp0001", BANNED), at=LATER, submitted=False),
        pick(("wp0005", PICKED), ("wp0006", PASSED)),
        pick(("wp0007", PASSED), ("wp0008", PASSED), none_of_these=True),
        pick(("wp0009", PickTile(favourite=True)), ("wp0010", PICKED), at=LATER),
        order(("wp0011", ranked(1)), ("wp0012", ranked(2)), ("wp0013", UNRANKED)),
        order(("wp0014", UNRANKED), ("wp0015", UNRANKED)),
        order(("wp0016", OrderTile(favourite=True)), ("wp0017", ranked(1, favourite=True)), at=LATER),
        gauntlet(("wp0018", WON), ("wp0019", LOST), streak=2),
        gauntlet(("wp0020", LOST), ("wp0018", LOST), streak=3, none_of_these=True),
        gauntlet(("wp0021", GauntletTile(picked=True, favourite=True)), ("wp0022", LOST), at=LATER),
    )

    decided = connection.execute(evidence_log.DECIDED).fetchall()

    assert sorted(str(row[0]) for row in decided) == [
        "wp0000",
        "wp0001",
        "wp0002",
        "wp0009",
        "wp0016",
        "wp0017",
        "wp0021",
    ]


def test_only_a_standing_explicit_verdict_is_explicitly_decided(connection: sqlite3.Connection) -> None:
    """The SQL spelling of "is an **Explicit Verdict**", over every mark and the legacy **Clearance**."""
    append(
        connection,
        grid(("wp0000", STARRED), ("wp0001", LIKED), ("wp0002", BANNED), ("wp0003", BLANK)),
        converted(("wp0004", LegacyEntry.LIKE)),
        converted(("wp0004", LegacyEntry.CLEARED), at=LATER),
        grid(("wp0005", STARRED)),
        grid(("wp0005", TileResponse(verdict=GridVerdict.IGNORE)), at=LATER, screen="history"),
    )

    assert evidence_log.explicitly_decided(connection) == {"wp0000", "wp0001", "wp0002"}


def test_favourites_are_what_resolves_to_favourite_now(connection: sqlite3.Connection) -> None:
    """Not what was ever a **Favourite**: any later **Judgement**, a **Clearance** included, takes it away."""
    append(
        connection,
        grid(("wp0003", STARRED), ("wp0001", STARRED), ("wp0002", LIKED)),
        converted(
            ("wp0004", LegacyEntry.FAVOURITE), ("wp0005", LegacyEntry.FAVOURITE), ("wp0006", LegacyEntry.BAN)
        ),
        grid(("wp0004", BLANK), ("wp0006", STARRED), at=LATER),
        converted(("wp0005", LegacyEntry.CLEARED), at=LATER),
    )

    assert evidence_log.favourites(connection) == ["wp0001", "wp0003", "wp0006"], "by id, for a seeded draw"


def test_the_lookalike_subjects_are_what_resolves_to_favourite_or_like_now(
    connection: sqlite3.Connection,
) -> None:
    """A **Like** stands beside a **Favourite** as a subject; a later **Ban**, **Ignore** or **Clearance**
    takes either away, and a **Ban** turned **Like** brings one in."""
    append(
        connection,
        grid(("wp0000", STARRED), ("wp0001", LIKED), ("wp0002", BANNED), ("wp0003", BLANK)),
        grid(("wp0006", LIKED), ("wp0005", STARRED), ("wp0004", BANNED), ("wp0007", LIKED), at=LATER),
        grid(("wp0006", BANNED), ("wp0004", LIKED), at=LATER + dt.timedelta(seconds=1), screen="history"),
        converted(("wp0007", LegacyEntry.CLEARED), at=LATER + dt.timedelta(seconds=1)),
    )

    assert evidence_log.lookalike_subjects(connection) == ["wp0000", "wp0001", "wp0004", "wp0005"], "by id"


def test_times_compared_counts_compare_screens_alone_whatever_was_answered(
    connection: sqlite3.Connection,
) -> None:
    """A **Line-up**'s wear (ADR 0028): picked, passed over, starred, "none of these", ranked or left
    unranked on an **Order**, or held or challenging on a **Gauntlet**, each count once per **Judgement**. A
    submitted **Batch**, a **Skip** and a **History** edit are not compare **Screens**."""
    append(
        connection,
        pick(("wp0000", PICKED), ("wp0001", PASSED)),
        pick(("wp0001", PASSED), ("wp0002", PASSED), none_of_these=True, at=LATER),
        pick(("wp0002", PickTile(favourite=True)), ("wp0001", PICKED), at=LATER + dt.timedelta(seconds=1)),
        order(("wp0006", ranked(1)), ("wp0001", UNRANKED), at=LATER + dt.timedelta(seconds=2)),
        order(("wp0006", UNRANKED), ("wp0002", UNRANKED), at=LATER + dt.timedelta(seconds=3)),
        gauntlet(("wp0006", WON), ("wp0000", LOST), at=LATER + dt.timedelta(seconds=4)),
        gauntlet(("wp0006", LOST), ("wp0001", LOST), at=LATER + dt.timedelta(seconds=5), none_of_these=True),
        grid(("wp0000", LIKED), ("wp0003", BLANK)),
        grid(("wp0003", BLANK), ("wp0004", STARRED), at=LATER, submitted=False),
        grid(("wp0005", BANNED), screen="history"),
    )

    asked = ["wp0001", "wp0000", "wp0002", "wp0003", "wp0004", "wp0005", "wp0006", "wp0001"]

    assert evidence_log.times_compared(connection, asked) == {
        "wp0000": 2,
        "wp0001": 5,
        "wp0002": 3,
        "wp0003": 0,
        "wp0004": 0,
        "wp0005": 0,
        "wp0006": 4,
    }
    assert evidence_log.times_compared(connection, []) == {}


# -- history -----------------------------------------------------------------------------------------------


def test_a_history_edit_is_a_submitted_judgement_of_that_one_wallpaper_on_the_history_screen(
    connection: sqlite3.Connection,
) -> None:
    """An **Ignore** chosen in **History** is recorded as one, a **Favourite** as the flag; the local offset
    comes from the clock."""
    clock = FakeClock(AT, offset=dt.timedelta(hours=-5))
    edit(connection, "wp0000", Verdict.IGNORE, clock)
    clock.advance(60)
    edit(connection, "wp0001", Verdict.FAVOURITE, clock)

    first, second = evidence_log.judgements(connection)

    assert first == grid(
        ("wp0000", TileResponse(verdict=GridVerdict.IGNORE)),
        screen="history",
        utc_offset=dt.timedelta(hours=-5),
    )
    assert (second.shown, second.recorded_at) == (
        (Shown(key("wp0001"), STARRED),),
        AT + dt.timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    ("submitted", "edits", "expected"),
    [
        pytest.param(
            BLANK, [Verdict.IGNORE], ResolvedVerdict(Verdict.IGNORE, -10), id="ignores do not stack"
        ),
        pytest.param(
            BLANK, [Verdict.IGNORE, Verdict.LIKE], ResolvedVerdict(Verdict.LIKE, 50), id="a verdict overturns"
        ),
        pytest.param(LIKED, [Verdict.FAVOURITE], ResolvedVerdict(Verdict.FAVOURITE, 100), id="replaced"),
        pytest.param(LIKED, [Verdict.IGNORE], ResolvedVerdict(Verdict.IGNORE, -10), id="an ignore withdraws"),
        pytest.param(STARRED, [Verdict.BAN], ResolvedVerdict(Verdict.BAN, -100), id="a ban overturns a star"),
    ],
)
def test_the_latest_judgement_decides_and_an_edit_in_the_same_moment_is_still_later(
    connection: sqlite3.Connection, submitted: TileResponse, edits: list[Verdict], expected: ResolvedVerdict
) -> None:
    """Submitted in a **Batch**, then edited from **History** under a clock that has not moved: each edit
    is recorded a microsecond after the **Judgement** before it, so the later act decides (invariant 4)."""
    append(connection, grid((SUBJECT, submitted), ("wp0001", BLANK)))

    for verdict in edits:
        assert isinstance(edit(connection, SUBJECT, verdict), HistoryEntry)

    assert resolved(connection, SUBJECT) == expected
    times = [j.recorded_at for j in evidence_log.judgements(connection)]
    assert times == [AT + n * evidence_log.ONE_MICROSECOND for n in range(len(edits) + 1)]


def test_a_history_edit_after_the_clock_stepped_back_still_overturns(connection: sqlite3.Connection) -> None:
    append(connection, grid((SUBJECT, LIKED), at=LATER))

    edit(connection, SUBJECT, Verdict.BAN, FakeClock(AT))

    assert resolved(connection, SUBJECT).verdict is Verdict.BAN
    assert evidence_log.history_entry(connection, SUBJECT).decided_at == LATER + evidence_log.ONE_MICROSECOND  # pyright: ignore[reportOptionalMemberAccess]


def test_a_recorded_time_is_the_clocks_unless_an_item_on_it_was_judged_at_or_after_it(
    connection: sqlite3.Connection,
) -> None:
    append(connection, grid(("wp0000", LIKED), at=LATER), grid(("wp0001", LIKED), at=AT, submitted=False))

    with storage.write(connection) as write:
        assert evidence_log.recorded_time(write, ["wp0002"], AT) == AT
        assert (
            evidence_log.recorded_time(write, ["wp0002", "wp0001"], AT) == AT + evidence_log.ONE_MICROSECOND
        )
        assert (
            evidence_log.recorded_time(write, ["wp0001", "wp0000"], AT)
            == LATER + evidence_log.ONE_MICROSECOND
        )
        assert evidence_log.recorded_time(write, [], AT) == AT


def test_editing_an_unknown_wallpaper_is_refused_and_appends_nothing(connection: sqlite3.Connection) -> None:
    """A refusal in the house style rather than the trigger's `IntegrityError`."""
    refused = edit(connection, "never-seen", Verdict.IGNORE)

    assert refused == HistoryRefused(reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER)
    assert evidence_log.judgements(connection) == []


def test_history_is_one_row_per_wallpaper_newest_decision_first(connection: sqlite3.Connection) -> None:
    """One **Batch**'s lines share its time, and come last tile first; a legacy **Clearance** has a line,
    resolved to nothing; a **Skip** adds none."""
    append(connection, grid(("wp0000", LIKED), ("wp0001", BANNED), ("wp0002", STARRED)))
    edit(connection, "wp0001", Verdict.LIKE, FakeClock(LATER))
    append(connection, converted(("wp0002", LegacyEntry.CLEARED), at=LATER + dt.timedelta(seconds=1)))
    append(connection, grid(("wp0003", LIKED), at=LATER + dt.timedelta(seconds=2), submitted=False))

    listing = evidence_log.history(connection)

    assert [(e.wallpaper_id, e.resolved.verdict) for e in listing.entries] == [
        ("wp0002", None),
        ("wp0001", Verdict.LIKE),
        ("wp0000", Verdict.LIKE),
    ]
    assert (listing.total, listing.page, listing.pages) == (3, 1, 1)


def test_history_lists_one_batch_last_tile_first(connection: sqlite3.Connection) -> None:
    """As the **Decision log** listed one submission's entries, newest by sequence first."""
    append(connection, grid(("wp0003", LIKED), ("wp0000", LIKED), ("wp0002", LIKED)))

    assert [e.wallpaper_id for e in evidence_log.history(connection).entries] == [
        "wp0002",
        "wp0000",
        "wp0003",
    ]


def test_history_shows_the_deciding_judgements_time_and_id(connection: sqlite3.Connection) -> None:
    first = grid((SUBJECT, LIKED))
    deciding = grid((SUBJECT, BANNED), at=LATER, screen="history")
    append(connection, first, deciding)

    (entry,) = evidence_log.history(connection).entries

    assert (entry.decided_at, entry.judgement_id) == (LATER, deciding.id)


def test_the_filter_narrows_to_one_resolved_verdict_and_the_count_matches_it(
    connection: sqlite3.Connection,
) -> None:
    """By what each **Wallpaper** resolves to now: the **Banned** one was **Liked** first."""
    append(
        connection,
        grid(("wp0000", LIKED), ("wp0001", LIKED), ("wp0002", STARRED)),
        grid(("wp0001", BANNED), ("wp0002", BLANK), ("wp0003", BLANK), at=LATER),
    )

    def listed(verdict: Verdict) -> tuple[list[str], int]:
        listing = evidence_log.history(connection, verdict)
        return [e.wallpaper_id for e in listing.entries], listing.total

    assert listed(Verdict.LIKE) == (["wp0000"], 1)
    assert listed(Verdict.BAN) == (["wp0001"], 1)
    assert listed(Verdict.FAVOURITE) == ([], 0)
    assert listed(Verdict.IGNORE) == (["wp0003", "wp0002"], 2)
    assert evidence_log.history(connection).total == 4


def test_history_pages_by_a_hundred_and_clamps_a_page_past_either_end(connection: sqlite3.Connection) -> None:
    for n, wallpaper_id in enumerate(WALLPAPERS):
        append(connection, grid((wallpaper_id, LIKED), at=AT + dt.timedelta(seconds=n)))

    first = evidence_log.history(connection)
    second = evidence_log.history(connection, page=2)

    assert (first.total, first.pages, len(first.entries)) == (HISTORY_PAGE_SIZE + 1, 2, HISTORY_PAGE_SIZE)
    assert first.entries[0].wallpaper_id == WALLPAPERS[-1]
    assert [e.wallpaper_id for e in second.entries] == [WALLPAPERS[0]]
    assert (first.previous_page, first.next_page) == (None, 2)
    assert (second.previous_page, second.next_page) == (1, None)
    assert evidence_log.history(connection, page=99).page == 2
    assert evidence_log.history(connection, page=0).page == 1


def test_one_wallpapers_line_is_the_line_the_page_shows(connection: sqlite3.Connection) -> None:
    """What a **History** edit swaps back in, resolved and dated as the whole page would show it."""
    append(connection, grid((SUBJECT, LIKED), ("wp0001", BANNED)), grid((SUBJECT, STARRED), at=LATER))

    assert evidence_log.history_entry(connection, SUBJECT) == evidence_log.history(connection).entries[0]
    assert evidence_log.history_entry(connection, "wp0002") is None


def test_an_empty_log_has_one_empty_page(connection: sqlite3.Connection) -> None:
    listing = evidence_log.history(connection, Verdict.LIKE)

    assert (listing.entries, listing.total, listing.page, listing.pages) == ((), 0, 1, 1)


def test_the_history_filter_offers_every_verdict_once_with_ignore_last() -> None:
    """**Ignore**, the commonest, goes last, after the ones worth looking for."""
    assert sorted(evidence_log.HISTORY_FILTERS) == sorted(Verdict)
    assert evidence_log.HISTORY_FILTERS[-1] is Verdict.IGNORE
    assert evidence_log.HISTORY_FILTERS[:2] == (Verdict.FAVOURITE, Verdict.LIKE)


def test_a_grid_mark_maps_onto_tastes_tile_at_the_edge() -> None:
    """wallpapi's four-word `Verdict` still offers **Favourite** as a choice; `taste/` has it as a flag."""
    assert evidence_log.tile_response(None) == BLANK
    assert evidence_log.tile_response(Verdict.FAVOURITE) == STARRED
    assert evidence_log.tile_response(Verdict.LIKE) == LIKED
    assert evidence_log.tile_response(Verdict.BAN) == BANNED
    assert evidence_log.tile_response(Verdict.IGNORE) == TileResponse(verdict=GridVerdict.IGNORE)
