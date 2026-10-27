"""Migrations that change what an existing database holds. Each test stops `migrate` at the version before the
step, arranges what an older database held at that point, and lets the rest of the steps run.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import BatchesRig, batches_rig, raw_connection, write_log_entries, write_old_pool
from tests.fakes import catalogue_of
from wallpapi import evidence_log, lineup, pool, settings, storage
from wallpapi.batches import zone_of
from wallpapi.model import Verdict, Zone
from wallpapi.settings import (
    RETUNED_SIMILARITY_RADIUS,
    SUPERSEDED_POOL_TARGET_SIZE,
    SUPERSEDED_SIMILARITY_RADIUS,
)
from wallpapi.taste.clock import iso_utc
from wallpapi.taste.evidence import AbsoluteEvidence
from wallpapi.taste.judgement import Judgement, Shown
from wallpapi.taste.ranking import rank
from wallpapi.taste.verdict_grid import VERDICT_GRID_V0, LegacyEntry, LegacyTileResponse

RETUNED = [
    # (setting, the default it replaced, a value a user chose, the version before the migration)
    pytest.param("similarity_radius", SUPERSEDED_SIMILARITY_RADIUS, 0.42, 8, id="radius, migration 9"),
    pytest.param("pool_target_size", SUPERSEDED_POOL_TARGET_SIZE, 3000, 9, id="pool target, migration 10"),
    pytest.param("similarity_radius", RETUNED_SIMILARITY_RADIUS, 0.2, 10, id="radius, migration 11"),
]


def _migrated(db_path: Path) -> settings.Settings:
    """Migrate the database at `db_path` the whole way, as a start-up does, and read its settings."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection)
        return settings.get(connection)


def test_a_fresh_database_seeds_a_similarity_radius_of_a_tenth(db_path: Path) -> None:
    """ADR 0023: 0.15 left no **Unknown** zone once the **Decision log** had grown."""
    assert _migrated(db_path).similarity_radius == 0.10


def _stored_at(db_path: Path, version: int, setting: str, value: float) -> None:
    """A database migrated to `version`, holding `value` for `setting` as the settings page stores it."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=version)
        connection.execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), setting))


@pytest.mark.parametrize(("setting", "superseded", "chosen", "before"), RETUNED)
def test_a_new_default_reaches_a_database_that_still_held_the_old_one(
    tmp_path: Path, setting: str, superseded: float, chosen: float, before: int
) -> None:
    """Holding the old default is exactly the state an older database is in."""
    db_path = tmp_path / "old.db"
    _stored_at(db_path, before, setting, superseded)

    migrated = _migrated(db_path)
    fresh = _migrated(tmp_path / "fresh.db")

    assert getattr(migrated, setting) == getattr(fresh, setting)
    assert getattr(migrated, setting) != superseded


@pytest.mark.parametrize(("setting", "superseded", "chosen", "before"), RETUNED)
def test_a_value_the_user_chose_is_left_exactly_as_they_set_it(
    db_path: Path, setting: str, superseded: float, chosen: float, before: int
) -> None:
    """A migration never undoes a setting: it rewrites only a row still holding the old default."""
    del superseded
    _stored_at(db_path, before, setting, chosen)

    assert getattr(_migrated(db_path), setting) == chosen


def test_migration_retires_every_pool_member_the_decision_log_already_mentions(db_path: Path) -> None:
    """Decide once (ADR 0016) at its third enforcement point: an older **Pool** still holds what was decided
    from **History**, which takes nothing out of the **Pool**; a legacy **Clearance** is a mention like any
    other entry."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=9)
    write_old_pool(db_path, catalogue_of(12))
    write_log_entries(db_path, {"wp0000": "like", "wp0001": "ignore", "wp0002": "cleared"})

    with raw_connection(db_path) as connection:
        storage.migrate(connection)
        members = {w.id for w in pool.members(connection)}

    assert len(members) == 9
    assert not members & {"wp0000", "wp0001", "wp0002"}


def test_migration_deletes_the_revisit_weight_row(db_path: Path) -> None:
    """Gone with ADR 0016, so its row goes rather than lingering. Read behind the seam because no setting
    names it any more."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=9)
        connection.execute("INSERT INTO settings (key, value) VALUES ('revisit_weight', '0.2')")

        storage.migrate(connection)

        assert connection.execute("SELECT * FROM settings WHERE key = 'revisit_weight'").fetchall() == []


# -- migration 12: the Decision log converted into the Evidence log ------------------------------------------

T = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.UTC)
MICROSECOND = dt.timedelta(microseconds=1)
X, Y, Z = "wp0000", "wp0001", "wp0002"


@dataclass(frozen=True, slots=True)
class OldBatch:
    """A submitted **Batch** as an older database held it: its tiles by position, each with the **Zone** it
    was drawn from or `None`, as a **Batch** minted before migration 6 has."""

    id: str
    created_at: dt.datetime
    tiles: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class OldEntry:
    seq: int
    wallpaper_id: str
    verdict: str
    at: dt.datetime
    batch_id: str | None = None


def _old_log(db_path: Path, batches: Sequence[OldBatch], entries: Sequence[OldEntry]) -> None:
    """A database at version 11 holding these **Batches** and **Decision log** entries, written as the old
    code wrote them (`iso_utc` times), every **Wallpaper** among them recorded. A database already at 11 is
    added to."""
    named = {e.wallpaper_id for e in entries} | {w for b in batches for w, _ in b.tiles}
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=11)
        with storage.write(connection) as write:
            write.executemany(
                "INSERT OR IGNORE INTO wallpapers"
                " VALUES (?, 3840, 2160, '1.78', 'general', 'sfw', 1, '', '', '', '')",
                [(w,) for w in sorted(named)],
            )
            for batch in batches:
                submitted = max(e.at for e in entries if e.batch_id == batch.id)
                write.execute(
                    "INSERT INTO batches (id, created_at, size, submitted_at) VALUES (?, ?, ?, ?)",
                    (batch.id, iso_utc(batch.created_at), len(batch.tiles), iso_utc(submitted)),
                )
                write.executemany(
                    "INSERT INTO batch_wallpapers (batch_id, wallpaper_id, position, zone)"
                    " VALUES (?, ?, ?, ?)",
                    [(batch.id, w, position, zone) for position, (w, zone) in enumerate(batch.tiles)],
                )
            write.executemany(
                "INSERT INTO decision_log (seq, wallpaper_id, batch_id, verdict, recorded_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [(e.seq, e.wallpaper_id, e.batch_id, e.verdict, iso_utc(e.at)) for e in entries],
            )


def _converted(db_path: Path) -> list[Judgement]:
    with raw_connection(db_path) as connection:
        storage.migrate(connection)
        return evidence_log.judgements(connection)


def _v0(
    screen: str,
    *tiles: tuple[str, LegacyEntry, dict[str, str]],
    at: dt.datetime,
    first_shown_at: dt.datetime | None = None,
) -> Judgement:
    return Judgement(
        instrument=VERDICT_GRID_V0,
        screen=screen,
        shown=tuple(
            Shown(evidence_log.item_key(w), LegacyTileResponse(entry), zone) for w, entry, zone in tiles
        ),
        recorded_at=at,
        app="wallpapi",
        app_version=evidence_log.APP_VERSION,
        first_shown_at=first_shown_at,
    )


def test_each_batch_becomes_one_judgement_in_position_order_and_each_lone_entry_one_of_its_own(
    db_path: Path,
) -> None:
    """ADR 0027's conversion: the verdict grid version 0, the **Batch**'s tiles in on-screen order with
    the stored **Zone** as **Provenance** where there is one, its mint time as the first-shown time; an
    entry with no **Batch** a **History** **Judgement**; every entry in its old words, a **Clearance**
    kept, every time kept."""
    batch = OldBatch("b1", T - dt.timedelta(minutes=1), ((Z, "banger"), (X, None), (Y, "unknown")))
    _old_log(
        db_path,
        [batch],
        [
            OldEntry(1, X, "ignore", T, "b1"),
            OldEntry(2, Y, "like", T, "b1"),
            OldEntry(3, Z, "favourite", T, "b1"),
            OldEntry(4, Y, "ban", T + dt.timedelta(minutes=5)),
            OldEntry(5, Z, "cleared", T + dt.timedelta(minutes=6, microseconds=7)),
        ],
    )

    assert _converted(db_path) == [
        _v0(
            "batch",
            (Z, LegacyEntry.FAVOURITE, {"zone": "banger"}),
            (X, LegacyEntry.IGNORE, {}),
            (Y, LegacyEntry.LIKE, {"zone": "unknown"}),
            at=T,
            first_shown_at=T - dt.timedelta(minutes=1),
        ),
        _v0("history", (Y, LegacyEntry.BAN, {}), at=T + dt.timedelta(minutes=5)),
        _v0("history", (Z, LegacyEntry.CLEARED, {}), at=T + dt.timedelta(minutes=6, microseconds=7)),
    ]


def test_a_same_time_tie_moves_the_later_by_old_sequence_on_a_microsecond_and_a_cascade_keeps_order(
    db_path: Path,
) -> None:
    """Every entry at one moment, as a frozen clock or one fast session writes them. Each **Judgement**
    moves only as far as it must to come after the one before it by old sequence on every **Item** it
    shares, so the time order on each **Item** is the old sequence order and each stands as it did.

    Worked by hand: the edit of X ties the first **Batch** (+1). The second **Batch** shows X again (before
    ADR 0016) and ties that edit's moved time (+2), and the edit of Z then ties it (+3). The edit of Y ties
    only the first **Batch** (+1), sharing a time with X's edit on another **Item**, which is no tie."""
    _old_log(
        db_path,
        [OldBatch("b1", T, ((X, None), (Y, None))), OldBatch("b2", T, ((X, None), (Z, None)))],
        [
            OldEntry(1, X, "like", T, "b1"),
            OldEntry(2, Y, "ignore", T, "b1"),
            OldEntry(3, X, "ignore", T),
            OldEntry(4, X, "ban", T + MICROSECOND, "b2"),
            OldEntry(5, Z, "like", T + MICROSECOND, "b2"),
            OldEntry(6, Z, "ignore", T + 2 * MICROSECOND),
            OldEntry(7, Y, "favourite", T),
        ],
    )

    moved = {
        tuple(evidence_log.wallpaper_id(item) for item in j.shown_set): j.recorded_at - T
        for j in _converted(db_path)
    }

    assert moved == {
        (X, Y): dt.timedelta(0),
        (X,): MICROSECOND,
        (Y,): MICROSECOND,
        (X, Z): 2 * MICROSECOND,
        (Z,): 3 * MICROSECOND,
    }
    with raw_connection(db_path) as connection:
        standing = evidence_log.resolve(connection, [X, Y, Z])
    assert {w: s.verdict for w, s in standing.items()} == {
        X: Verdict.BAN,
        Y: Verdict.FAVOURITE,
        Z: Verdict.IGNORE,
    }


def test_an_entry_recorded_before_the_one_it_followed_moves_to_just_after_it(db_path: Path) -> None:
    """A clock stepped back between a **Batch** and its **History** edit: by sequence the edit decided, so
    it is moved to stay later, rather than the **Batch** overturning it."""
    _old_log(
        db_path,
        [OldBatch("b1", T, ((X, None),))],
        [OldEntry(1, X, "like", T + dt.timedelta(seconds=10), "b1"), OldEntry(2, X, "ban", T)],
    )

    assert [j.recorded_at for j in _converted(db_path)] == [
        T + dt.timedelta(seconds=10),
        T + dt.timedelta(seconds=10) + MICROSECOND,
    ]


def test_a_batch_whose_entries_disagree_on_time_is_recorded_at_the_latest_of_them(db_path: Path) -> None:
    """A **Batch** is one submission, so one time; the latest is when the last of it was written."""
    _old_log(
        db_path,
        [OldBatch("b1", T, ((X, None), (Y, None)))],
        [OldEntry(1, X, "like", T + dt.timedelta(seconds=2), "b1"), OldEntry(2, Y, "ban", T, "b1")],
    )

    assert [j.recorded_at for j in _converted(db_path)] == [T + dt.timedelta(seconds=2)]


def test_a_batch_entry_with_no_tile_row_comes_after_the_placed_tiles_in_old_sequence(db_path: Path) -> None:
    """Not a state the old code could make: kept, in an order that does not depend on the database."""
    _old_log(
        db_path,
        [OldBatch("b1", T, ((Y, "dud"),))],
        [OldEntry(1, Z, "like", T, "b1"), OldEntry(2, Y, "ban", T, "b1"), OldEntry(3, X, "ignore", T, "b1")],
    )

    (converted,) = _converted(db_path)

    assert [(s.item, dict(s.provenance)) for s in converted.shown] == [
        (evidence_log.item_key(Y), {"zone": "dud"}),
        (evidence_log.item_key(Z), {}),
        (evidence_log.item_key(X), {}),
    ]


ONE_OF_EACH = (
    [OldBatch("b1", T - dt.timedelta(minutes=2), ((X, "unknown"), (Y, "banger")))],
    [
        OldEntry(1, X, "ignore", T, "b1"),
        OldEntry(2, Y, "favourite", T, "b1"),
        OldEntry(3, X, "like", T),
        OldEntry(4, Y, "cleared", T + dt.timedelta(seconds=30)),
    ],
)


def test_two_copies_of_one_log_convert_to_the_same_ids(tmp_path: Path) -> None:
    for name in ("one.db", "another.db"):
        _old_log(tmp_path / name, *ONE_OF_EACH)

    ids = [[j.id for j in _converted(tmp_path / name)] for name in ("one.db", "another.db")]

    assert ids[0] == ids[1]
    assert len(set(ids[0])) == 3


def test_the_converted_ids_are_pinned(db_path: Path) -> None:
    """A regression pin, recorded when migration 12 was written: a converted log's ids are what a later
    merge recognises it by, so a change to `taste/`'s canonical form or to what the conversion builds that
    moves them must fail here, not on someone's database."""
    _old_log(db_path, *ONE_OF_EACH)
    edit_of_x = (
        '{"format":1,"instrument":{"name":"verdict grid","version":0},'
        '"recorded_at":"2026-10-01T12:00:00.000001+00:00","response":null,"screen":"history",'
        '"shown":[{"item":"wallhaven:wp0000","response":{"entry":"like"}}]}'
    )

    converted = _converted(db_path)

    assert [j.id for j in converted] == PINNED_IDS
    assert converted[1].id == hashlib.sha256(edit_of_x.encode("ascii")).hexdigest(), "spelled by hand"


PINNED_IDS = [
    "a4bf396059f5230c577e7a34fbcbd340f7e2312affb1b70a737128d82d51e7a3",
    "2a00c6528e6b7525d700138c4a1beb434d4f8dcb96ec9c7dbcb058d423cc2ce3",
    "01e9787d757804d8daf43acd8ecc7ee19f4267553964909bc0cf30273e3c6832",
]
"""The **Batch**, the edit of X moved one microsecond off the **Batch**'s time, and the **Clearance**."""


def test_the_decision_log_and_the_batches_are_left_exactly_as_they_were(db_path: Path) -> None:
    """Frozen, not dropped (ADR 0027): nothing about the history is rewritten. Every column the tables
    had; `batches` gains `mix`, `NULL` for a **Batch** minted before it."""
    _old_log(db_path, *ONE_OF_EACH)
    queries = (
        "SELECT seq, wallpaper_id, batch_id, verdict, recorded_at FROM decision_log ORDER BY seq",
        "SELECT id, created_at, size, submitted_at FROM batches ORDER BY id",
        "SELECT batch_id, wallpaper_id, position, zone FROM batch_wallpapers ORDER BY batch_id, position",
    )
    with raw_connection(db_path) as connection:
        before = [[tuple(row) for row in connection.execute(query)] for query in queries]

        storage.migrate(connection)

        after = [[tuple(row) for row in connection.execute(query)] for query in queries]
        mixes = [row[0] for row in connection.execute("SELECT mix FROM batches")]
    assert after == before
    assert len(before[0]) == 4
    assert mixes == [None]


def test_the_users_log_at_its_size_converts_in_well_under_a_second(db_path: Path) -> None:
    """About 100 **Batches** of 8 and 18 **History** edits: the live database's 818 entries."""
    batches = [OldBatch(f"b{n:03d}", T, tuple((f"wp{n:03d}{t}", None) for t in range(8))) for n in range(100)]
    entries = [
        OldEntry(n * 8 + t + 1, w, "ignore", T + dt.timedelta(minutes=n), b.id)
        for n, b in enumerate(batches)
        for t, (w, _) in enumerate(b.tiles)
    ]
    entries += [OldEntry(801 + n, f"wp{n:03d}0", "like", T + dt.timedelta(days=1)) for n in range(18)]
    _old_log(db_path, batches, entries)

    with raw_connection(db_path) as connection:
        started = time.perf_counter()
        storage.migrate(connection)
        took = time.perf_counter() - started
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM judgements), (SELECT COUNT(*) FROM judgement_items)"
        ).fetchone()

    assert tuple(counts) == (118, 818)
    assert took < 0.5


OLD_VALUES = {"favourite": 100.0, "like": 50.0, "ignore": -10.0, "ban": -100.0, "cleared": 0.0}
"""What each **Decision log** entry resolved to before the **Evidence log**, `cleared` standing at nothing."""


def _scored_as_the_decision_log_scored(rig: BatchesRig, db_path: Path) -> dict[str, tuple[float, Zone]]:
    """The **Scores** the code before migration 12 gave: the latest entry by sequence decides, one piece of
    Evidence per decided **Wallpaper** in id order, a **Ban** never a candidate, through the same ranking."""
    with raw_connection(db_path) as connection:
        rows = connection.execute("SELECT wallpaper_id, verdict FROM decision_log ORDER BY seq").fetchall()
    latest = {str(row["wallpaper_id"]): str(row["verdict"]) for row in rows}
    judged = sorted(latest)
    candidates = [w for w in pool.members(rig.connection) if latest.get(w.id) != "ban"]
    wallpapers = {w.id: w for w in [*candidates, *pool.wallpapers(rig.connection, judged).values()]}
    old_evidence = [
        AbsoluteEvidence(
            item=w, strength=OLD_VALUES[latest[w]], judgement_id=f"decision-log:{w}", recorded_at=T
        )
        for w in judged
    ]
    current = settings.get(rig.connection)
    ranking = rank(
        old_evidence,
        [w.id for w in candidates],
        lambda rows, columns: rig.embeddings.similarities(
            [wallpapers[r] for r in rows], [wallpapers[c] for c in columns]
        ),
        radius=current.similarity_radius,
        decay=current.similarity_decay,
    )
    return {w.id: (score, zone_of(score)) for w, score in zip(candidates, ranking.scores, strict=True)}


def test_scores_and_zones_are_the_same_after_the_conversion(db_path: Path) -> None:
    """Story 35: ranking reads the converted log and every **Score** comes out to the bit as before. Most
    entries share one moment, so only the conversion's microsecond moves keep the old sequence's answers:
    the **Favourite** edit over the **Like**, the **Ignore** edit over the **Ban**, a reshown **Like** over
    the **Ignore** before it. A **Clearance** stands at nothing, and a **Ban** still in the **Pool** is no
    candidate.

    Bit for bit, which needs the decided columns in the old id order: the **Like** recorded earliest is the
    first the log names, and the similarities near it, the **Favourite** and the **Ignore** were found by
    search to sum one ulp apart in the two orders.

    Worked by hand as well: near the **Favourite** a **Banger**, near the **Ignore** a **Dud**, near the
    reshown **Like** a **Banger**, near the **Clearance** an **Unknown**, near the **Favourite**, the
    **Ignore** and the early **Like** a **Banger**."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=11)
    write_old_pool(db_path, catalogue_of(11))
    _old_log(
        db_path,
        [
            OldBatch("b1", T, (("wp0000", None), ("wp0001", None), ("wp0002", None))),
            OldBatch("b2", T, (("wp0003", None), ("wp0002", None))),
        ],
        [
            OldEntry(1, "wp0000", "like", T, "b1"),
            OldEntry(2, "wp0001", "ban", T, "b1"),
            OldEntry(3, "wp0002", "ignore", T, "b1"),
            OldEntry(4, "wp0000", "favourite", T),
            OldEntry(5, "wp0001", "ignore", T),
            OldEntry(6, "wp0003", "favourite", T, "b2"),
            OldEntry(7, "wp0002", "like", T, "b2"),
            OldEntry(8, "wp0003", "cleared", T + dt.timedelta(seconds=1)),
            OldEntry(9, "wp0004", "ban", T),
            OldEntry(10, "wp0010", "like", T - dt.timedelta(seconds=1)),
        ],
    )
    with raw_connection(db_path) as connection, storage.write(connection) as write:
        pool.retire(write, ["wp0000", "wp0001", "wp0002", "wp0003", "wp0010"])
    near = {
        ("wp0005", "wp0000"): 0.95,
        ("wp0006", "wp0001"): 0.95,
        ("wp0007", "wp0002"): 0.95,
        ("wp0008", "wp0003"): 0.95,
        ("wp0009", "wp0000"): 0.91,
        ("wp0009", "wp0001"): 0.92,
        ("wp0009", "wp0010"): 0.92,
    }

    with batches_rig(db_path, near) as rig:
        after = {s.wallpaper.id: (s.score, s.zone) for s in rig.batches.classify(rig.connection)}
        before = _scored_as_the_decision_log_scored(rig, db_path)

    assert after == before
    assert {w: zone for w, (_, zone) in after.items()} == {
        "wp0005": Zone.BANGER,
        "wp0006": Zone.DUD,
        "wp0007": Zone.BANGER,
        "wp0008": Zone.UNKNOWN,
        "wp0009": Zone.BANGER,
    }


def test_a_batch_live_across_migration_13_has_no_slot_zone_stamp_or_preview_and_submits_without_them(
    db_path: Path,
) -> None:
    """A **Batch** minted at version 12 knows the **Zone** each tile came from and its **Mix**, but not its
    **Slots** or its **Ranking stamp**: they are left absent, never guessed, and its **Judgement** says so."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=12)
        with storage.write(connection) as write:
            write.execute(
                "INSERT INTO wallpapers"
                " VALUES ('wp0000', 3840, 2160, '1.78', 'general', 'sfw', 1, '', '', '', '')"
            )
            write.execute(
                "INSERT INTO batches (id, created_at, size, mix) VALUES ('b1', ?, 1, 'explore')",
                (iso_utc(T),),
            )
            write.execute(
                "INSERT INTO batch_wallpapers (batch_id, wallpaper_id, position, zone)"
                " VALUES ('b1', 'wp0000', 0, 'unknown')"
            )

    with batches_rig(db_path) as rig:
        live = rig.next()
        rig.submit("b1")
        (judgement,) = rig.logged()

    assert (live.id, live.zones, live.slots, live.ranking, live.previewed) == (
        "b1",
        {"wp0000": Zone.UNKNOWN},
        {},
        None,
        frozenset(),
    )
    assert [shown.provenance for shown in judgement.shown] == [{"zone": "unknown", "mix": "explore"}]
    assert judgement.ranking is None


def test_a_lineup_live_across_migration_17_has_no_champion_and_a_lineup_holds_at_most_one(
    db_path: Path,
) -> None:
    """A **Line-up** drawn at version 16 was drawn whole: every tile reads back with no streak, so it has no
    champion. After it, the schema itself refuses a second champion on one **Line-up**, and a streak below
    1."""
    with raw_connection(db_path) as connection:
        storage.migrate(connection, to=16)
        with storage.write(connection) as write:
            write.executemany(
                "INSERT INTO wallpapers VALUES (?, 3840, 2160, '1.78', 'general', 'sfw', 1, '', '', '', '')",
                [("wp0000",), ("wp0001",)],
            )
            write.execute(
                "INSERT INTO lineups (id, created_at, mix) VALUES ('l1', ?, 'explore')", (iso_utc(T),)
            )
            write.executemany(
                "INSERT INTO lineup_wallpapers (lineup_id, wallpaper_id, position, zone, slot_zone)"
                " VALUES ('l1', ?, ?, 'unknown', 'unknown')",
                [("wp0000", 0), ("wp0001", 1)],
            )
        storage.migrate(connection)

        live = lineup.live(connection)
        assert live is not None
        assert (live.id, live.champion, live.streak) == ("l1", None, 0)

        with storage.write(connection) as write:
            write.execute("UPDATE lineup_wallpapers SET streak = 2 WHERE wallpaper_id = 'wp0000'")
        live = lineup.live(connection)
        assert live is not None
        assert (live.champion, live.streak) == ("wp0000", 2)
        for refused in (
            "streak = 1 WHERE wallpaper_id = 'wp0001'",
            "streak = 0 WHERE wallpaper_id = 'wp0000'",
        ):
            with pytest.raises(sqlite3.IntegrityError), storage.write(connection) as write:
                write.execute(f"UPDATE lineup_wallpapers SET {refused}")
