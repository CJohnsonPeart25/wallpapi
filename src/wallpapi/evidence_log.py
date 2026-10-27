"""The **Evidence log**: appending **Judgements**, reading them back, and resolving what each **Item** stands
at. The one piece of data wallpapi cannot rebuild, and nothing else reads or writes `judgements` or
`judgement_items` (ADR 0027).

`taste/` defines the **Judgement** and stores nothing; this module keeps one row per **Judgement** and one per
shown **Item**, made by `storage`'s migrations 12 and 14. Items are keyed by source, `wallhaven:<id>`, and
this is the edge where wallpapi's bare Wallhaven ids become keys and back: every other table keeps the bare
id. A **Response** field SQL reads has a column; the others are kept as JSON in `response`, and only reading
a **Judgement** back opens it.

**Verdict resolution** is spelled twice. `taste.resolution.resolve` over `taste.reductions.evidence` is the
rule; `_RESOLUTION_CTE` is the SQL **History** pages and filters with, and with it a second spelling of the
absolute part of the **Reductions**: a version 0 grid tile says its converted entry, and a version 1 tile on
a submitted screen says its **Favourite** or its **Verdict**, an unmarked one an **Ignore**, while a screen
not submitted (a **Skip**) says nothing; a **Pick** says **Favourite** of a starred tile and nothing absolute
of any other. The tests hold the SQL to both `EXAMPLE_CASES` and to `evidence()`.
"""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from wallpapi.model import Verdict, Zone
from wallpapi.taste.clock import Clock
from wallpapi.taste.judgement import (
    Context,
    Instrument,
    ItemResponse,
    Judgement,
    JudgementResponse,
    LogPosition,
    Provenance,
    RankingStamp,
    Shown,
    instant,
)
from wallpapi.taste.pick import PICK_V1, PickResponse, PickTile
from wallpapi.taste.verdict_grid import (
    FAVOURITE_STRENGTH,
    LEGACY_STRENGTHS,
    VERDICT_GRID_V0,
    VERDICT_GRID_V1,
    GridResponse,
    LegacyEntry,
    LegacyTileResponse,
    TileResponse,
)
from wallpapi.taste.verdict_grid import Verdict as GridVerdict

ITEM_SOURCE: Final = "wallhaven"
"""The only **Item source** built: every key in the log is `wallhaven:<id>`."""

APP: Final = "wallpapi"
APP_VERSION: Final = importlib.metadata.version("wallpapi")
"""What each **Judgement** names as the app that recorded it, a converted one included."""

SCREEN_BATCH: Final = "batch"
SCREEN_HISTORY: Final = "history"
SCREEN_COMPARE: Final = "compare"
"""The **Screens** wallpapi asks on: the verdict grid, **History**, and the compare tab's **Line-up**."""

PROVENANCE_ZONE: Final = "zone"
"""The **Zone** an **Item** was drawn from."""

PROVENANCE_SLOT_ZONE: Final = "slot_zone"
"""The **Zone** the **Slot** an **Item** filled was meant for: another than `zone` after a **Shortfall**."""

PROVENANCE_MIX: Final = "mix"
"""The name of the **Mix** the screen was drawn with."""

CONTEXT_THEME: Final = "theme"
"""The **Context** field naming the page's theme as the browser resolved it: `Theme`'s values."""


class Theme(StrEnum):
    """The page's theme when a screen was answered, `system` already resolved by the browser. Never a
    setting: it describes the moment, and is recorded only where the page sent it."""

    LIGHT = "light"
    DARK = "dark"


HISTORY_PAGE_SIZE = 100
"""Rows on one page of **History**: it grows by hundreds of **Ignores** a week and needs *a* bound."""

HISTORY_FILTERS = (Verdict.FAVOURITE, Verdict.LIKE, Verdict.BAN, Verdict.IGNORE)
"""The **History** filter's choices in display order, written out so **Ignore**, the commonest, is last.
The `Verdict` enum keeps its own order.
"""

ONE_MICROSECOND: Final = dt.timedelta(microseconds=1)
"""The step between two **Judgements** on one **Item** that would otherwise share a recorded time."""


@dataclass(frozen=True, slots=True)
class ResolvedVerdict:
    """What one **Wallpaper** stands at: the `verdict` to show, in the grid's four words, and the `value`
    its standing strength is worth."""

    verdict: Verdict | None
    value: float


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One **Wallpaper**'s line in **History**, not one **Judgement**: what it resolves to, and when and by
    which **Judgement** that was decided."""

    wallpaper_id: str
    resolved: ResolvedVerdict
    decided_at: dt.datetime
    judgement_id: str


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


@dataclass(frozen=True)
class HistoryRefused:
    """A **History** edit did not happen, and this is why. Only a hand-made post or a second tab can cause
    one.
    """

    class Reason(StrEnum):
        UNKNOWN_WALLPAPER = "unknown_wallpaper"
        """No such **Wallpaper** in this database."""

    reason: Reason


def item_key(wallpaper_id: str) -> str:
    """The key `taste/` and the log name a Wallhaven **Wallpaper** by: its id namespaced by source."""
    return f"{ITEM_SOURCE}:{wallpaper_id}"


def wallpaper_id(key: str) -> str:
    """The bare Wallhaven id behind a key. A key from another source is refused with `ValueError`."""
    source, separator, bare = key.partition(":")
    if source != ITEM_SOURCE or not separator:
        raise ValueError(f"{key!r} is not from the {ITEM_SOURCE} item source")
    return bare


def provenance(*, zone: Zone | None, slot_zone: Zone | None, mix: str | None) -> Provenance:
    """Why a drawn tile was on its screen, in wallpapi's labels: the **Zone** it was drawn from, the **Zone**
    its **Slot** was meant for, and the **Mix**, each only where the screen knows it. For every screen drawn
    from a **Mix**, a **Batch** or a **Line-up**."""
    labels = {PROVENANCE_ZONE: zone, PROVENANCE_SLOT_ZONE: slot_zone, PROVENANCE_MIX: mix}
    return {name: str(label) for name, label in labels.items() if label is not None}


def context(*, theme: Theme | None) -> Context:
    """The **Context** of a screen answered on the page: the theme where the page sent one, and nothing for
    what it did not. For every screen the page answers, a **Batch** or a **Line-up**."""
    return {} if theme is None else {CONTEXT_THEME: theme.value}


def tile_response(verdict: Verdict | None, *, preview_opened: bool = False) -> TileResponse:
    """A grid tile's version 1 **Response** from wallpapi's four-word `Verdict`: a **Favourite** is the flag,
    the other three are `taste/`'s **Verdicts**, and `None` is an unmarked tile. `preview_opened` sits
    beside whichever it is."""
    if verdict is None:
        return TileResponse(preview_opened=preview_opened)
    if verdict is Verdict.FAVOURITE:
        return TileResponse(favourite=True, preview_opened=preview_opened)
    return TileResponse(verdict=GridVerdict(verdict.value), preview_opened=preview_opened)


def append(write: sqlite3.Connection, judgement: Judgement) -> bool:
    """Append one **Judgement** inside the caller's write transaction, and say whether it was new.

    All or nothing: a **Judgement** naming a Wallhaven id this database never recorded raises
    `sqlite3.IntegrityError` and leaves the caller's transaction as it was. The same **Judgement** appended
    twice is held once, its id being its content. An **Item** from another source, or an **Instrument** the
    log has no columns for, is refused with `ValueError` before anything is written.
    """
    for shown in judgement.shown:
        wallpaper_id(shown.item)
    rows = [
        (shown.item, *_item_columns(judgement, shown), _json(shown.provenance)) for shown in judgement.shown
    ]
    submitted, response = _screen_columns(judgement)
    write.execute("SAVEPOINT append_judgement")
    try:
        inserted = write.execute(
            _INSERT_JUDGEMENT,
            (
                judgement.id,
                judgement.format_version,
                judgement.instrument.name,
                judgement.instrument.version,
                judgement.screen,
                instant(judgement.recorded_at),
                None if judgement.utc_offset is None else _whole_seconds(judgement.utc_offset),
                None if judgement.first_shown_at is None else instant(judgement.first_shown_at),
                judgement.app,
                judgement.app_version,
                submitted,
                response,
                None if judgement.ranking is None else stamp_text(judgement.ranking),
                _json(judgement.context),
            ),
        ).rowcount
        if inserted:
            write.executemany(
                _INSERT_ITEM, [(judgement.id, position, *row) for position, row in enumerate(rows)]
            )
    except BaseException:
        write.execute("ROLLBACK TO append_judgement")
        write.execute("RELEASE append_judgement")
        raise
    write.execute("RELEASE append_judgement")
    return bool(inserted)


def judgements(connection: sqlite3.Connection) -> list[Judgement]:
    """Every **Judgement**, rebuilt from its rows: by recorded time, and on an exact tie the lower id last,
    so the last on an **Item** is the one that decides it.

    Each is hashed again as it is built, and one whose rows no longer earn the id they are stored under
    raises `ValueError`: a stored id is never trusted.
    """
    shown: defaultdict[str, list[Shown]] = defaultdict(list)
    for row in connection.execute(_SELECT_ITEMS):
        shown[str(row["judgement_id"])].append(
            Shown(
                item=str(row["item"]),
                response=_item_response(_instrument_of(row), row),
                provenance=_provenance(row["provenance"]),
            )
        )
    found: list[Judgement] = []
    for row in connection.execute(_SELECT_JUDGEMENTS):
        stored = str(row["id"])
        instrument = _instrument_of(row)
        judgement = Judgement(
            instrument=instrument,
            screen=str(row["screen"]),
            shown=tuple(shown[stored]),
            recorded_at=_moment(row["recorded_at"]),
            app=str(row["app"]),
            app_version=str(row["app_version"]),
            response=_judgement_response(instrument, row),
            utc_offset=None if row["utc_offset"] is None else dt.timedelta(seconds=int(row["utc_offset"])),
            first_shown_at=None if row["first_shown_at"] is None else _moment(row["first_shown_at"]),
            ranking=None if row["ranking"] is None else stamp_from_text(row["ranking"]),
            context=_context(row["context"]),
            format_version=int(row["format_version"]),
        )
        if judgement.id != stored:
            raise ValueError(
                f"the Judgement stored as {stored} does not earn that id: it hashes to {judgement.id}"
            )
        found.append(judgement)
    return found


def recorded_time(
    connection: sqlite3.Connection, wallpaper_ids: Sequence[str], now: dt.datetime
) -> dt.datetime:
    """When a **Judgement** of these **Wallpapers** made `now` is recorded: `now`, or one microsecond after
    the latest **Judgement** already on any of them if that is not before `now`.

    The latest recorded time decides, so a later act must never share or precede an earlier one's time on
    the same **Item**: a **History** edit made in the microsecond its **Batch** was submitted, or after the
    clock stepped back, still overturns it. Read inside the caller's write transaction.
    """
    keys = [item_key(w) for w in dict.fromkeys(wallpaper_ids)]
    if not keys:
        return now
    placeholders = ",".join("?" * len(keys))
    latest = connection.execute(
        f"SELECT MAX(j.recorded_at) FROM judgement_items AS i JOIN judgements AS j ON j.id = i.judgement_id"
        f" WHERE i.item IN ({placeholders})",
        keys,
    ).fetchone()[0]
    if latest is None:
        return now
    return max(now, _moment(latest) + ONE_MICROSECOND)


def edit(
    write: sqlite3.Connection, wallpaper_id: str, verdict: Verdict, *, clock: Clock
) -> HistoryEntry | HistoryRefused:
    """Change a **Wallpaper**'s standing from **History**: a submitted version 1 **Judgement** of that one
    **Wallpaper** on the history **Screen**, inside the caller's write transaction, and the line **History**
    now shows for it. An **Ignore** chosen here is recorded as one; a **Favourite** is the flag.

    A **Wallpaper** this database has never recorded is refused, by the log's trigger rather than by reading
    another module's table.
    """
    now = clock.now()
    at = recorded_time(write, [wallpaper_id], now)
    judgement = Judgement(
        instrument=VERDICT_GRID_V1,
        screen=SCREEN_HISTORY,
        shown=(Shown(item_key(wallpaper_id), tile_response(verdict)),),
        recorded_at=at,
        app=APP,
        app_version=APP_VERSION,
        response=GridResponse(submitted=True),
        utc_offset=clock.utc_offset(now),
    )
    try:
        append(write, judgement)
    except sqlite3.IntegrityError:
        return HistoryRefused(reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER)
    entry = history_entry(write, wallpaper_id)
    if entry is None:
        raise AssertionError("a Judgement was appended for this Wallpaper and must resolve")
    return entry


def resolve(connection: sqlite3.Connection, wallpaper_ids: Sequence[str]) -> dict[str, ResolvedVerdict]:
    """**Verdict resolution** for many **Wallpapers** at once, in one query, keyed by every id asked about.

    Nothing standing, a legacy **Clearance** last, and an unknown id all resolve to no **Verdict** worth zero.
    """
    requested = list(dict.fromkeys(wallpaper_ids))
    resolved = dict.fromkeys(requested, _ABSENT)
    if not requested:
        return resolved
    # SQLite's parameter limit is 32,766 here, well above any **Pool** this is handed.
    placeholders = ",".join("?" * len(requested))
    query = _resolution_query(
        "SELECT wallpaper_id, resolved FROM resolution", restriction=f"AND i.item IN ({placeholders})"
    )
    for row in connection.execute(query, [item_key(w) for w in requested]).fetchall():
        resolved[str(row["wallpaper_id"])] = _resolved_from(row["resolved"])
    return resolved


def history(connection: sqlite3.Connection, verdict: Verdict | None = None, page: int = 1) -> History:
    """One page of **History**, newest decision first, optionally narrowed to one resolved **Verdict** in
    SQL. `page` is clamped rather than refused, so a stale link shows the last page.
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
    """One **Wallpaper**'s line in **History**, as the page shows it, or `None` if nothing stands for it."""
    row = connection.execute(_HISTORY_ENTRY, (item_key(wallpaper_id),)).fetchone()
    return None if row is None else _history_entry_from(row)


def favourites(connection: sqlite3.Connection) -> list[str]:
    """Every **Wallpaper** that resolves to a **Favourite**, by id so a seeded draw repeats."""
    query = _resolution_query("SELECT wallpaper_id FROM resolution WHERE resolved = ? ORDER BY wallpaper_id")
    return [str(row["wallpaper_id"]) for row in connection.execute(query, (Verdict.FAVOURITE.value,))]


def lookalike_subjects(connection: sqlite3.Connection) -> list[str]:
    """Every **Wallpaper** that resolves to a **Favourite** or a **Like**: what a **Lookalike search** may be
    about (ADR 0022). By id, so the grouping and a seeded draw repeat.
    """
    query = _resolution_query(
        "SELECT wallpaper_id FROM resolution WHERE resolved IN (?, ?) ORDER BY wallpaper_id"
    )
    subjects = (Verdict.FAVOURITE.value, Verdict.LIKE.value)
    return [str(row["wallpaper_id"]) for row in connection.execute(query, subjects)]


def explicitly_decided(connection: sqlite3.Connection) -> set[str]:
    """Every **Wallpaper** with an **Explicit Verdict** standing. The only spelling of "explicit"."""
    return {str(row["wallpaper_id"]) for row in connection.execute(_EXPLICITLY_DECIDED)}


_ABSENT = ResolvedVerdict(verdict=None, value=0.0)
"""A **Wallpaper** with nothing standing: never decided, or a legacy **Clearance** last."""


def _resolved_from(resolved: object) -> ResolvedVerdict:
    """What a resolved word is worth, by `taste/`'s strengths. The rule that *chose* it is
    `_RESOLUTION_CTE`, never copied here."""
    if resolved is None:
        return _ABSENT
    verdict = Verdict(str(resolved))
    if verdict is Verdict.FAVOURITE:
        return ResolvedVerdict(verdict=verdict, value=FAVOURITE_STRENGTH)
    return ResolvedVerdict(verdict=verdict, value=LEGACY_STRENGTHS[LegacyEntry(verdict.value)])


def _history_entry_from(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        wallpaper_id=str(row["wallpaper_id"]),
        resolved=_resolved_from(row["resolved"]),
        decided_at=_moment(row["decided_at"]),
        judgement_id=str(row["judgement_id"]),
    )


# -- the columns a Response is kept in ---------------------------------------------------------------------


type _ItemColumns = tuple[str | None, int, int, str | None, str | None]
"""`verdict`, `favourite`, `preview_opened`, `entry`, and `response`, the JSON of the fields no SQL reads."""

type _ScreenColumns = tuple[int | None, str | None]
"""`submitted`, and `response`, the JSON of the fields no SQL reads."""


def _item_columns(judgement: Judgement, shown: Shown) -> _ItemColumns:
    """One tile's **Response** as its columns, or `ValueError` for a shape the log cannot keep. A field SQL
    reads has a column; the rest go in `response`, which nothing but this module's reading back opens."""
    response = shown.response
    if judgement.instrument == VERDICT_GRID_V1 and isinstance(response, TileResponse):
        verdict = None if response.verdict is None else response.verdict.value
        return verdict, int(response.favourite), int(response.preview_opened), None, None
    if judgement.instrument == VERDICT_GRID_V0 and isinstance(response, LegacyTileResponse):
        return None, 0, 0, response.entry.value, None
    if judgement.instrument == PICK_V1 and isinstance(response, PickTile):
        # No preview on a **Line-up**, so none was opened.
        return None, int(response.favourite), 0, None, _json({_PICKED: response.picked})
    raise _not_kept(judgement, f"a tile response of {response!r}")


def _screen_columns(judgement: Judgement) -> _ScreenColumns:
    """The judgement-level **Response** as its columns, as `_item_columns` keeps a tile's."""
    response = judgement.response
    if judgement.instrument == VERDICT_GRID_V1 and isinstance(response, GridResponse):
        return int(response.submitted), None
    if judgement.instrument == VERDICT_GRID_V0 and response is None:
        return None, None
    if judgement.instrument == PICK_V1 and isinstance(response, PickResponse):
        return None, _json({_NONE_OF_THESE: response.none_of_these})
    raise _not_kept(judgement, f"a screen response of {response!r}")


_PICKED: Final = "picked"
_NONE_OF_THESE: Final = "none_of_these"
"""**Pick**'s field names in `response`: `PickTile.picked` and `PickResponse.none_of_these`."""


def _not_kept(judgement: Judgement, what: str) -> ValueError:
    instrument = judgement.instrument
    return ValueError(f"the Evidence log keeps no {what} for {instrument.name} version {instrument.version}")


def _instrument_of(row: sqlite3.Row) -> Instrument:
    return Instrument(str(row["instrument"]), int(row["instrument_version"]))


def _item_response(instrument: Instrument, row: sqlite3.Row) -> ItemResponse:
    if instrument == VERDICT_GRID_V0:
        return LegacyTileResponse(entry=LegacyEntry(str(row["entry"])))
    if instrument == VERDICT_GRID_V1:
        return TileResponse(
            verdict=None if row["verdict"] is None else GridVerdict(str(row["verdict"])),
            favourite=bool(row["favourite"]),
            preview_opened=bool(row["preview_opened"]),
        )
    if instrument == PICK_V1:
        return PickTile(picked=bool(_fields(row)[_PICKED]), favourite=bool(row["favourite"]))
    raise _not_read(instrument)


def _judgement_response(instrument: Instrument, row: sqlite3.Row) -> JudgementResponse | None:
    if instrument == VERDICT_GRID_V0:
        return None
    if instrument == VERDICT_GRID_V1:
        return GridResponse(submitted=bool(row["submitted"]))
    if instrument == PICK_V1:
        return PickResponse(none_of_these=bool(_fields(row)[_NONE_OF_THESE]))
    raise _not_read(instrument)


def _fields(row: sqlite3.Row) -> dict[str, object]:
    """The `response` column's fields, by name."""
    return dict(json.loads(str(row["response"])))


def _not_read(instrument: Instrument) -> ValueError:
    return ValueError(f"the Evidence log cannot read {instrument.name} version {instrument.version}")


def _whole_seconds(offset: dt.timedelta) -> int:
    seconds = offset.total_seconds()
    if seconds != int(seconds):
        raise ValueError(f"a UTC offset is kept in whole seconds, not {offset}")
    return int(seconds)


def stamp_text(stamp: RankingStamp) -> str:
    """A **Ranking stamp** as the **Evidence log** keeps it, JSON text: also how a screen waiting to be
    answered keeps the stamp it was drawn under until its **Judgement** carries it."""
    read = stamp.read
    fields = {
        "read": None
        if read is None
        else {"recorded_at": instant(read.recorded_at), "judgement_id": read.judgement_id},
        "space": stamp.space,
        "radius": stamp.radius,
        "decay": stamp.decay,
        "distillation": stamp.distillation,
        "lens": stamp.lens,
    }
    return _json(fields)


def stamp_from_text(stored: object) -> RankingStamp:
    """The **Ranking stamp** `stamp_text` wrote."""
    fields = json.loads(str(stored))
    read = fields["read"]
    return RankingStamp(
        read=None
        if read is None
        else LogPosition(recorded_at=_moment(read["recorded_at"]), judgement_id=str(read["judgement_id"])),
        space=str(fields["space"]),
        radius=float(fields["radius"]),
        decay=float(fields["decay"]),
        distillation=fields["distillation"],
        lens=fields["lens"],
    )


def _provenance(stored: object) -> Provenance:
    return {str(name): str(label) for name, label in json.loads(str(stored)).items()}


def _context(stored: object) -> Context:
    return dict(json.loads(str(stored)))


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _moment(stored: object) -> dt.datetime:
    return dt.datetime.fromisoformat(str(stored))


_INSERT_JUDGEMENT = """
INSERT INTO judgements (
    id, format_version, instrument, instrument_version, screen, recorded_at, utc_offset, first_shown_at,
    app, app_version, submitted, response, ranking, context
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO NOTHING
"""

_INSERT_ITEM = """
INSERT INTO judgement_items (
    judgement_id, position, item, verdict, favourite, preview_opened, entry, response, provenance
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_JUDGEMENTS = "SELECT * FROM judgements ORDER BY recorded_at, id DESC"

_SELECT_ITEMS = """
SELECT i.*, j.instrument, j.instrument_version
FROM judgement_items AS i
JOIN judgements AS j ON j.id = i.judgement_id
ORDER BY i.judgement_id, i.position
"""


# -- resolution in SQL -------------------------------------------------------------------------------------


_KEY_START = len(ITEM_SOURCE) + 2
"""Where the bare id starts in a key, counting from 1 as SQLite's `substr` does."""

_SAYS_ABSOLUTE = f"""
(
    (j.instrument = '{VERDICT_GRID_V1.name}'
        AND (j.instrument_version = {VERDICT_GRID_V0.version}
            OR (j.instrument_version = {VERDICT_GRID_V1.version} AND j.submitted = 1)))
    OR (j.instrument = '{PICK_V1.name}' AND j.instrument_version = {PICK_V1.version} AND i.favourite = 1)
)
"""
"""The tiles that are absolute Evidence: every tile of a converted grid **Judgement** and of a version 1
grid screen that was submitted, and a starred tile on a **Pick**. A **Skip** says nothing about any **Item**
on it, and a **Pick** nothing about a tile it did not star: what it says of the rest is relative."""

DECIDED = f"""
SELECT DISTINCT substr(i.item, {_KEY_START})
FROM judgement_items AS i
JOIN judgements AS j ON j.id = i.judgement_id
WHERE {_SAYS_ABSOLUTE}
"""
"""Every **Wallpaper** that has ever had a **Verdict** or a **Favourite** recorded, by bare id, as a subquery
for SQL elsewhere to compose: what the **Pool** refuses (ADR 0016, ADR 0027). An unmarked tile on a submitted
**Batch** is an **Ignore** and counts; a legacy **Clearance** counts, though it resolves to nothing; an
**Item** only ever on a **Skip** does not, nor one only ever on a **Line-up** unless it was starred there.
Asked of the log, not of resolution, because a **Clearance** resolves to the same nothing as a **Wallpaper**
never decided.
"""

_RESOLUTION_CTE = f"""
WITH said AS (
    SELECT
        i.item,
        i.position,
        j.id AS judgement_id,
        j.recorded_at,
        CASE
            WHEN j.instrument = '{VERDICT_GRID_V0.name}' AND j.instrument_version = {VERDICT_GRID_V0.version}
                THEN i.entry
            WHEN i.favourite = 1 THEN '{Verdict.FAVOURITE.value}'
            ELSE COALESCE(i.verdict, '{Verdict.IGNORE.value}')
        END AS said
    FROM judgement_items AS i
    JOIN judgements AS j ON j.id = i.judgement_id
    WHERE {_SAYS_ABSOLUTE}
    {{restriction}}
),
latest AS (
    SELECT said.*, ROW_NUMBER() OVER (PARTITION BY item ORDER BY recorded_at DESC, judgement_id) AS nth
    FROM said
),
resolution AS (
    SELECT
        substr(item, {_KEY_START}) AS wallpaper_id,
        judgement_id,
        position,
        recorded_at AS decided_at,
        NULLIF(said, '{LegacyEntry.CLEARED.value}') AS resolved
    FROM latest
    WHERE nth = 1
)
"""
"""**Verdict resolution**, as the prelude to every query that needs it (ADR 0015, ADR 0027).

`said` is what each tile that says anything absolute says, in the grid's four words or a legacy `cleared`:
the second spelling of the **Reductions** the module docstring names. Only a starred tile on a **Pick** says
anything absolute, and it says **Favourite**. The **Judgement** with the
latest recorded time decides, and an exact tie falls to the lower **Judgement** id; times are fixed-width
UTC text, so text order is time order. A legacy **Clearance** as the latest resolves to `NULL`. In SQL
because **History** filters and pages over the resolved **Verdict**.

`{{restriction}}` is an `AND` built here, never caller text; values are bound as parameters.
"""


def _resolution_query(select: str, *, restriction: str = "") -> str:
    """A query over the resolved **Evidence log**: the shared CTE, then whatever is selected from it."""
    return _RESOLUTION_CTE.format(restriction=restriction) + select


_HISTORY_COLUMNS = "wallpaper_id, resolved, decided_at, judgement_id"

_HISTORY_ORDER = "ORDER BY decided_at DESC, judgement_id, position DESC"
"""Newest decision first. One **Judgement**'s lines share its time and id, and come last tile first, as the
**Decision log** listed one submission's entries; two **Judgements** sharing a time come lower id first, the
order resolution puts them in."""


def _history_query(*, filtered: bool) -> str:
    where = "WHERE resolved = ?" if filtered else ""
    return _resolution_query(
        f"SELECT {_HISTORY_COLUMNS} FROM resolution {where}\n{_HISTORY_ORDER}\nLIMIT ? OFFSET ?"
    )


def _history_count_query(*, filtered: bool) -> str:
    """How many lines the same filter matches, for the paging."""
    where = "WHERE resolved = ?" if filtered else ""
    return _resolution_query(f"SELECT COUNT(*) FROM resolution {where}")


_HISTORY_ENTRY = _resolution_query(f"SELECT {_HISTORY_COLUMNS} FROM resolution", restriction="AND i.item = ?")

_EXPLICITLY_DECIDED = _resolution_query(
    f"SELECT wallpaper_id FROM resolution WHERE resolved IS NOT NULL AND resolved != '{Verdict.IGNORE.value}'"
)
"""Every **Wallpaper** with an **Explicit Verdict** standing: never evicted, and pre-marked when reshown."""
