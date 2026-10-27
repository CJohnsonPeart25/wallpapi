"""The **Line-up**: drawing one for the compare tab, keeping it and its stars until it is answered, and
answering it with a **Pick** into the **Evidence log**. Nothing else reads or writes `lineups`,
`lineup_wallpapers` or `lineup_stars`.

A **Line-up** is drawn as a **Batch** is, from the classified **Pool** `Batches.ranked` gives, by the active
**Mix**'s allocation (`allocation.draw`), at the **Line-up** size, but from the **Pool** less the live
**Batch**'s tiles, so one **Wallpaper** is never asked about on both tabs at once. There is one live
**Line-up**, kept until it is answered (ADR 0002's rule) or discarded by a **Pool reset** (ADR 0025), so a
reload shows the same one. Answering records one **Judgement** and drops the **Line-up**, whose
**Judgement** then holds all of it; the next is drawn when it is asked for.

A **Pick** says one tile beat the others, which is relative Evidence, and a "none of these" says nothing:
neither decides anything (ADR 0027). The star is the one thing on a **Line-up** that does. Any tile may be
starred, and a star is held against its tile of the live **Line-up** (`star`) until the **Line-up** is
answered, so a reload keeps it; held per tile, not per **Instrument**, so every **Instrument** on a
**Line-up** stars the same way. Answering records each star in the **Judgement** as a **Favourite** and
retires that **Wallpaper** from the **Pool** (ADR 0016); every other tile stays.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final
from uuid import uuid4

from wallpapi import batches, evidence_log, pool, settings, storage
from wallpapi.allocation import draw
from wallpapi.batches import Ranked
from wallpapi.evidence_log import Theme
from wallpapi.model import Wallpaper, Zone
from wallpapi.pool import RefillStatus, wallpaper_from_row
from wallpapi.similarity import Embeddings
from wallpapi.taste.clock import Clock, iso_utc
from wallpapi.taste.judgement import Judgement, Provenance, RankingStamp, Shown
from wallpapi.taste.pick import PICK_V1, PickResponse, PickTile
from wallpapi.taste.rng import SeededRandom

FEWEST: Final = 2
"""The fewest **Wallpapers** a **Line-up** compares: one compares with nothing."""


@dataclass(frozen=True, slots=True)
class Lineup:
    """The **Wallpapers** on the compare tab, with the identity an answer quotes back.

    `zones` is the **Zone** each was drawn from at mint time and `slots` the **Zone** its **Slot** was meant
    for; `mix` the name of the **Mix** it was drawn with; `ranking` the **Ranking stamp** it was drawn under,
    `None` when nothing ranked it. All recorded at mint, never recomputed. `starred` is every tile starred
    so far, held until the **Line-up** is answered.
    """

    id: str
    created_at: dt.datetime
    wallpapers: tuple[Wallpaper, ...]
    zones: Mapping[str, Zone]
    slots: Mapping[str, Zone]
    mix: str
    ranking: RankingStamp | None
    starred: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LineupUnavailable:
    """No **Line-up** could be drawn: fewer than two **Wallpapers** in the **Pool** outside the live
    **Batch**, and the page needs to say whether that is waiting or Wallhaven being unreachable."""

    class Reason(StrEnum):
        TOO_FEW = "too_few"
        """Fewer than two to compare, and the refill has not failed: the **Pool** is filling, or the live
        **Batch** is showing what there is."""

        WALLHAVEN_UNREACHABLE = "wallhaven_unreachable"
        """Too few, and the last refill attempt failed. `error` says how."""

    reason: Reason
    error: str | None = None
    error_at: dt.datetime | None = None


@dataclass(frozen=True)
class LineupRefused:
    """The answer was not recorded, and this is why. Never a silent no-op: two tabs, or a number key pressed
    twice, is a real case."""

    class Reason(StrEnum):
        UNKNOWN_LINEUP = "unknown_lineup"
        """Not the live **Line-up**: already answered, discarded by a **Pool reset**, or never drawn here."""

        NOT_IN_LINEUP = "not_in_lineup"
        """A winner, or a star, on a tile the **Line-up** does not show: no control posts it."""

    reason: Reason


@dataclass(frozen=True, slots=True)
class Picked:
    """What a **Pick** recorded: the **Line-up** answered, its winner, `None` for "none of these", and the
    tiles starred on it, each now a **Favourite**, in on-screen order."""

    lineup_id: str
    winner: str | None
    starred: tuple[str, ...] = ()


def _new_id() -> str:
    return uuid4().hex


class Lineups:
    """Draws and answers **Line-ups**: the parts that need the ranking a **Batch** is drawn from, the
    **Embeddings** for the varied **Unknown** draw, the Refill's status, the clock, the draw's own random
    source or the id source."""

    def __init__(
        self,
        ranked: Callable[[sqlite3.Connection], Ranked],
        embeddings: Embeddings,
        refill_status: Callable[[], RefillStatus],
        clock: Clock,
        random_source: SeededRandom,
        *,
        new_id: Callable[[], str] = _new_id,
    ) -> None:
        self._ranked = ranked
        self._embeddings = embeddings
        self._refill_status = refill_status
        self._clock = clock
        self._random = random_source
        self._new_id = new_id

    def next(self, connection: sqlite3.Connection) -> Lineup | LineupUnavailable:
        """The live **Line-up**, drawing one only if there is none. No **API call**.

        Ranked outside the write transaction, which stays short, then re-read under the write lock so two
        tabs opened at once cannot each draw one.
        """
        existing = live(connection)
        if existing is not None:
            return existing

        ranked = self._ranked(connection)
        on_the_batch = batches.showing(connection)
        candidates = [scored for scored in ranked.scored if scored.wallpaper.id not in on_the_batch]
        if len(candidates) < FEWEST:
            return self._too_few()
        vectors = self._embeddings.vectors([scored.wallpaper for scored in candidates])
        mix = settings.active_mix(connection)
        chosen = draw(mix, candidates, settings.get(connection).lineup_size, self._random, vectors)
        created_at = self._clock.now()
        lineup_id = self._new_id()

        with storage.write(connection) as write:
            contended = live(write)
            if contended is not None:
                return contended
            write.execute(
                "INSERT INTO lineups (id, created_at, mix, ranking) VALUES (?, ?, ?, ?)",
                (
                    lineup_id,
                    iso_utc(created_at),
                    mix.name,
                    None if ranked.stamp is None else evidence_log.stamp_text(ranked.stamp),
                ),
            )
            write.executemany(
                "INSERT INTO lineup_wallpapers (lineup_id, wallpaper_id, position, zone, slot_zone)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (lineup_id, tile.wallpaper.id, position, tile.zone.value, tile.slot.value)
                    for position, tile in enumerate(chosen)
                ],
            )

        return Lineup(
            id=lineup_id,
            created_at=created_at,
            wallpapers=tuple(tile.wallpaper for tile in chosen),
            zones={tile.wallpaper.id: tile.zone for tile in chosen},
            slots={tile.wallpaper.id: tile.slot for tile in chosen},
            mix=mix.name,
            ranking=ranked.stamp,
        )

    def pick(
        self, write: sqlite3.Connection, lineup_id: str, winner: str | None, *, theme: Theme | None = None
    ) -> Picked | LineupRefused:
        """Answer the live **Line-up** `lineup_id` with `winner`, or "none of these" with `None`, inside the
        caller's write transaction, and drop it; or say why not, before anything is written.

        One **Pick** version 1 **Judgement** on the compare **Screen**: every tile in on-screen order, the
        winner marked and each star as a **Favourite**, each with its **Provenance**, the **Ranking stamp**
        it was drawn under, its mint time as first shown, the clock's local offset, and the page's `theme` in
        the **Context** where it sent one. A starred tile is decided, so it leaves the **Pool**; nothing
        else does.
        """
        claimed = _claim(write, lineup_id)
        if claimed is None:
            return LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
        shown = [w.id for w in claimed.wallpapers]
        if winner is not None and winner not in shown:
            return LineupRefused(reason=LineupRefused.Reason.NOT_IN_LINEUP)
        starred = tuple(w for w in shown if w in claimed.starred)
        now = self._clock.now()
        evidence_log.append(
            write,
            Judgement(
                instrument=PICK_V1,
                screen=evidence_log.SCREEN_COMPARE,
                shown=tuple(
                    Shown(
                        evidence_log.item_key(w),
                        PickTile(picked=w == winner, favourite=w in claimed.starred),
                        _provenance(claimed, w),
                    )
                    for w in shown
                ),
                recorded_at=evidence_log.recorded_time(write, shown, now),
                app=evidence_log.APP,
                app_version=evidence_log.APP_VERSION,
                response=PickResponse(none_of_these=winner is None),
                utc_offset=self._clock.utc_offset(now),
                first_shown_at=claimed.created_at,
                ranking=claimed.ranking,
                context=evidence_log.context(theme=theme),
            ),
        )
        pool.retire(write, starred)
        _drop(write, "?", (lineup_id,))
        return Picked(lineup_id=lineup_id, winner=winner, starred=starred)

    def _too_few(self) -> LineupUnavailable:
        """Why there was nothing to compare. A recorded refill failure outranks "nothing yet"."""
        status = self._refill_status()
        if status.last_error is not None:
            return LineupUnavailable(
                reason=LineupUnavailable.Reason.WALLHAVEN_UNREACHABLE,
                error=status.last_error,
                error_at=status.last_error_at,
            )
        return LineupUnavailable(reason=LineupUnavailable.Reason.TOO_FEW)


def live(connection: sqlite3.Connection) -> Lineup | None:
    """The live **Line-up**, rebuilt from storage, or `None`. Never draws one, unlike `Lineups.next`."""
    row = connection.execute(
        "SELECT id, created_at, mix, ranking FROM lineups ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return None if row is None else _lineup_from(connection, row)


def showing(connection: sqlite3.Connection) -> set[str]:
    """Every **Wallpaper** on the live **Line-up**."""
    return {
        str(row["wallpaper_id"]) for row in connection.execute("SELECT wallpaper_id FROM lineup_wallpapers")
    }


def star(
    write: sqlite3.Connection, lineup_id: str, wallpaper_id: str, *, starred: bool
) -> Lineup | LineupRefused:
    """Star one tile of the live **Line-up**, or take its star off with `starred=False`, inside the caller's
    write transaction, and answer with the **Line-up** as it now stands; or say why not, before anything is
    written.

    Sets rather than toggles, so a replayed click cannot flip it. Not an answer: nothing is recorded, nothing
    leaves the **Pool**, and the **Line-up** stays on screen until it is answered.
    """
    claimed = _claim(write, lineup_id)
    if claimed is None:
        return LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
    if wallpaper_id not in {w.id for w in claimed.wallpapers}:
        return LineupRefused(reason=LineupRefused.Reason.NOT_IN_LINEUP)
    if starred:
        write.execute(
            "INSERT INTO lineup_stars (lineup_id, wallpaper_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (lineup_id, wallpaper_id),
        )
        return replace(claimed, starred=claimed.starred | {wallpaper_id})
    write.execute(
        "DELETE FROM lineup_stars WHERE lineup_id = ? AND wallpaper_id = ?", (lineup_id, wallpaper_id)
    )
    return replace(claimed, starred=claimed.starred - {wallpaper_id})


def discard_live(write: sqlite3.Connection) -> int:
    """Drop the live **Line-up** inside the caller's transaction, and say how many were dropped: a **Pool
    reset**'s share (ADR 0025). Housekeeping, not an answer: nothing is recorded."""
    return _drop(write, "SELECT id FROM lineups")


def _claim(write: sqlite3.Connection, lineup_id: str) -> Lineup | None:
    row = write.execute(
        "SELECT id, created_at, mix, ranking FROM lineups WHERE id = ?", (lineup_id,)
    ).fetchone()
    return None if row is None else _lineup_from(write, row)


def _drop(write: sqlite3.Connection, selection: str, parameters: Sequence[str] = ()) -> int:
    """Delete the **Line-ups** whose ids the SQL `selection` gives (a subquery, or `?` for one), and say how
    many. Stars, then tiles, then the **Line-up**, for the foreign keys."""
    write.execute(f"DELETE FROM lineup_stars WHERE lineup_id IN ({selection})", parameters)
    write.execute(f"DELETE FROM lineup_wallpapers WHERE lineup_id IN ({selection})", parameters)
    return write.execute(f"DELETE FROM lineups WHERE id IN ({selection})", parameters).rowcount


def _lineup_from(connection: sqlite3.Connection, lineup: sqlite3.Row) -> Lineup:
    """A `lineups` row with its tiles and their stars."""
    lineup_id = str(lineup["id"])
    rows = connection.execute(_SELECT_LINEUP_WALLPAPERS, (lineup_id,)).fetchall()
    stars = connection.execute("SELECT wallpaper_id FROM lineup_stars WHERE lineup_id = ?", (lineup_id,))
    return Lineup(
        id=lineup_id,
        created_at=dt.datetime.fromisoformat(str(lineup["created_at"])),
        wallpapers=tuple(wallpaper_from_row(row) for row in rows),
        zones={str(row["id"]): Zone(str(row["zone"])) for row in rows},
        slots={str(row["id"]): Zone(str(row["slot_zone"])) for row in rows},
        mix=str(lineup["mix"]),
        ranking=None if lineup["ranking"] is None else evidence_log.stamp_from_text(lineup["ranking"]),
        starred=frozenset(str(row["wallpaper_id"]) for row in stars),
    )


def _provenance(lineup: Lineup, wallpaper_id: str) -> Provenance:
    """Why a tile was on the **Line-up**: the **Zone** it was drawn from, its **Slot**'s and the **Mix**."""
    return evidence_log.provenance(
        zone=lineup.zones[wallpaper_id], slot_zone=lineup.slots[wallpaper_id], mix=lineup.mix
    )


_SELECT_LINEUP_WALLPAPERS = """
SELECT w.*, lw.zone AS zone, lw.slot_zone AS slot_zone
FROM lineup_wallpapers AS lw
JOIN wallpapers AS w ON w.id = lw.wallpaper_id
WHERE lw.lineup_id = ?
ORDER BY lw.position
"""
