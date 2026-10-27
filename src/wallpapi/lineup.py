"""The **Line-up**: drawing one for each visit to the compare tab, holding its stars and an **Order**'s ranks
while it is live, and answering it with a **Pick**, an **Order** or a **Gauntlet** into the **Evidence log**.
Nothing else reads or writes `lineups`, `lineup_wallpapers`, `lineup_stars` or `lineup_ranks`.

A **Line-up** is drawn by the active **Mix** and **Wear** (ADR 0028), at the **Line-up** size, from the
classified **Pool** (`Batches.ranked`) less the live **Batch**'s tiles, so one **Wallpaper** is never asked
about on both tabs at once. The allocation and the **Shortfall** are a **Batch**'s (`allocation.draw`); wear
decides which **Wallpaper** fills each **Slot**, so a **Pick**, which moves no **Score**, does not bring the
same best **Banger** back on every screen. `wear` is the one place wear is worked out.

There is one live **Line-up**. Each visit draws a new one (`Lineups.fresh`), and the one it replaces is
dropped with its stars and records nothing (ADR 0029); a **Pool reset** discards it too (ADR 0025).
Answering records one **Judgement** and drops the **Line-up**, whose **Judgement** then holds all of it; the
next is drawn when it is asked for.

A **Pick** says one tile beat the others and an **Order** which beat which, both relative Evidence, and a
"none of these" or an empty **Order** says nothing: none of them decides anything (ADR 0027). The star is the
one thing on a **Line-up** that does. Any tile may be starred, and a star is held against its tile of the
live **Line-up** (`star`) for as long as the **Line-up** is live, which a reload ends; held per tile, not
per **Instrument**, so every **Instrument** on a **Line-up** stars the same way. Answering records each
star in the **Judgement** as a **Favourite** and retires that **Wallpaper** from the **Pool** (ADR 0016);
every other tile stays.

An **Order** is built a click at a time, each rank held against its tile of the live **Line-up**
(`Lineups.rank`) as a star is, and goes with it the same way. It is answered when all but the last tile are
ranked, the last place implied, or when it is submitted as it stands (`Lineups.order`).

A **Gauntlet** is answered as a **Pick** is (`Lineups.gauntlet`), and its winner is the champion of the next
**Line-up**: kept in its place, with one more screen to its streak if it was the champion already, while the
other tiles are drawn around it as any **Line-up**'s are (`Lineups.next` with `keeping`). The champion and
its streak are held on its tile of the live **Line-up**, so they last exactly as long as the screen: a
visit, a **Pool reset** or "none of these" ends the streak. A champion is kept only while it could be drawn:
in the **Pool**, off the live **Batch**, not a **Ban**'s near-duplicate. A winner starred on the screen it
won has left the **Pool** as a **Favourite**, so its streak ends there too, as does one a **Batch** decided
in the meantime.
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
from wallpapi.allocation import Drawn, ScoredWallpaper, Wear, draw
from wallpapi.batches import Ranked
from wallpapi.evidence_log import Theme
from wallpapi.model import Mix, Wallpaper, Zone
from wallpapi.pool import RefillStatus, wallpaper_from_row
from wallpapi.similarity import Embeddings
from wallpapi.taste.clock import Clock, iso_utc
from wallpapi.taste.gauntlet import GAUNTLET_V1, GauntletResponse, GauntletTile
from wallpapi.taste.judgement import (
    Instrument,
    ItemResponse,
    Judgement,
    JudgementResponse,
    Provenance,
    RankingStamp,
    Shown,
)
from wallpapi.taste.order import ORDER_V1, OrderTile
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
    so far and `ranked` every tile an **Order** in progress has ranked, best first, both held while the
    **Line-up** is live. `champion` is the tile a **Gauntlet** kept from the screen before, `None` on a
    **Line-up** drawn whole, and `streak` how many screens in a row it has won, the count its crown shows,
    0 with no champion.
    """

    id: str
    created_at: dt.datetime
    wallpapers: tuple[Wallpaper, ...]
    zones: Mapping[str, Zone]
    slots: Mapping[str, Zone]
    mix: str
    ranking: RankingStamp | None
    starred: frozenset[str] = frozenset()
    ranked: tuple[str, ...] = ()
    champion: str | None = None
    streak: int = 0

    def rank_of(self, wallpaper_id: str) -> int | None:
        """The tile's rank on the **Order** in progress, 1 the best, or `None` while it is unranked."""
        return self.ranked.index(wallpaper_id) + 1 if wallpaper_id in self.ranked else None


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
        """Not the live **Line-up**: already answered, replaced by a later visit (ADR 0029), discarded by a
        **Pool reset**, or never drawn here."""

        NOT_IN_LINEUP = "not_in_lineup"
        """A winner, a star or a rank on a tile the **Line-up** does not show: no control posts it."""

    reason: Reason


@dataclass(frozen=True, slots=True)
class Picked:
    """What a **Pick** recorded: the **Line-up** answered, its winner, `None` for "none of these", and the
    tiles starred on it, each now a **Favourite**, in on-screen order."""

    lineup_id: str
    winner: str | None
    starred: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Ordered:
    """What an **Order** recorded: the **Line-up** answered, its tiles ranked best first, the last place
    included where it was implied, empty for an **Order** answered with nothing ranked; and the tiles
    starred on it, each now a **Favourite**, in on-screen order."""

    lineup_id: str
    ranked: tuple[str, ...]
    starred: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Champion:
    """A **Gauntlet**'s winner, to keep its place on the next **Line-up**: the tile, the position it won at,
    how many screens in a row it has now won, and the **Zone** it was drawn from and its **Slot**'s on the
    screen it was first drawn onto, which its **Provenance** goes on giving."""

    wallpaper_id: str
    position: int
    streak: int
    zone: Zone
    slot: Zone


@dataclass(frozen=True, slots=True)
class Gauntleted:
    """What a **Gauntlet** recorded: the **Line-up** answered, its winner, `None` for "none of these", and the
    tiles starred on it, each now a **Favourite**, in on-screen order; and `champion`, the winner for the next
    **Line-up** to keep, `None` after "none of these". Whether it can still be kept is the next draw's to
    say (`Lineups.next`)."""

    lineup_id: str
    winner: str | None
    starred: tuple[str, ...] = ()
    champion: Champion | None = None


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

    def fresh(self, connection: sqlite3.Connection) -> Lineup | LineupUnavailable:
        """A new **Line-up** for a visit to the compare tab, replacing the live one (ADR 0029). No **API
        call**.

        The **Line-up** it replaces is dropped with its stars in the transaction that writes its successor,
        and records nothing: it adds no **Wear**, and its tiles stay in the **Pool**. With too few to draw,
        the live one is still dropped: a visit always ends the screen before it.
        """
        return self._mint(connection, replacing=True)

    def next(
        self, connection: sqlite3.Connection, *, keeping: Champion | None = None
    ) -> Lineup | LineupUnavailable:
        """The live **Line-up**, drawing one only if there is none: what follows an answer, or a refused one,
        never a visit. No **API call**.

        One drawn after a **Gauntlet** is `keeping` its champion: in the position it won at, or last if the
        **Line-up** size has shrunk below that, with the other tiles drawn around it, as many as the size
        less one and the candidates allow. A champion that could not be drawn now (it has left the **Pool**
        or is on the live **Batch**) is not kept, and the **Line-up** is drawn whole; so is one when a
        **Line-up** is live already, as after another tab drew it.
        """
        existing = live(connection)
        if existing is not None:
            return existing
        return self._mint(connection, replacing=False, keeping=keeping)

    def _mint(
        self, connection: sqlite3.Connection, *, replacing: bool, keeping: Champion | None = None
    ) -> Lineup | LineupUnavailable:
        """Draw a **Line-up**, around the champion it is `keeping` if it can, and store it, `replacing` the
        live one or, if another request drew one first, handing that back.

        Ranked outside the write transaction, which stays short; without `replacing`, re-read under the
        write lock so two requests at once cannot each draw one.
        """
        ranked = self._ranked(connection)
        on_the_batch = batches.showing(connection)
        candidates = [scored for scored in ranked.scored if scored.wallpaper.id not in on_the_batch]
        if len(candidates) < FEWEST:
            if replacing:
                with storage.write(connection) as write:
                    discard_live(write)
            return self._too_few()
        mix = settings.active_mix(connection)
        size = settings.get(connection).lineup_size
        kept = _keepable(keeping, candidates)
        if kept is None:
            chosen = self._drawn(connection, mix, candidates, size)
            champion, streak = None, 0
        else:
            held, at = kept
            challengers = [scored for scored in candidates if scored is not at]
            chosen = self._drawn(connection, mix, challengers, size - 1)
            chosen.insert(
                min(held.position, len(chosen)),
                Drawn(wallpaper=at.wallpaper, score=at.score, zone=held.zone, slot=held.slot),
            )
            champion, streak = held.wallpaper_id, held.streak
        created_at = self._clock.now()
        lineup_id = self._new_id()

        with storage.write(connection) as write:
            if replacing:
                discard_live(write)
            else:
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
                "INSERT INTO lineup_wallpapers (lineup_id, wallpaper_id, position, zone, slot_zone, streak)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        lineup_id,
                        tile.wallpaper.id,
                        position,
                        tile.zone.value,
                        tile.slot.value,
                        streak if tile.wallpaper.id == champion else None,
                    )
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
            champion=champion,
            streak=streak,
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
        if winner is not None and winner not in {w.id for w in claimed.wallpapers}:
            return LineupRefused(reason=LineupRefused.Reason.NOT_IN_LINEUP)
        starred = self._answer(
            write,
            claimed,
            PICK_V1,
            lambda w: PickTile(picked=w == winner, favourite=w in claimed.starred),
            PickResponse(none_of_these=winner is None),
            theme=theme,
        )
        return Picked(lineup_id=lineup_id, winner=winner, starred=starred)

    def gauntlet(
        self, write: sqlite3.Connection, lineup_id: str, winner: str | None, *, theme: Theme | None = None
    ) -> Gauntleted | LineupRefused:
        """Answer the live **Line-up** `lineup_id` with `winner`, or "none of these" with `None`, inside the
        caller's write transaction, and drop it; or say why not, before anything is written.

        One **Gauntlet** version 1 **Judgement**, recorded as a **Pick** is, with the streak the champion
        carried onto this screen, the count its crown showed. The winner is handed back as the champion for
        the next **Line-up** to keep where it won: one more screen to its streak if it was the champion here,
        a streak of 1 if it was a challenger. "None of these" hands back none, which ends the streak. A
        starred tile leaves the **Pool**; nothing else does.
        """
        claimed = _claim(write, lineup_id)
        if claimed is None:
            return LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
        shown = [w.id for w in claimed.wallpapers]
        if winner is not None and winner not in shown:
            return LineupRefused(reason=LineupRefused.Reason.NOT_IN_LINEUP)
        starred = self._answer(
            write,
            claimed,
            GAUNTLET_V1,
            lambda w: GauntletTile(picked=w == winner, favourite=w in claimed.starred),
            GauntletResponse(streak=claimed.streak, none_of_these=winner is None),
            theme=theme,
        )
        champion = (
            None
            if winner is None
            else Champion(
                wallpaper_id=winner,
                position=shown.index(winner),
                streak=claimed.streak + 1 if winner == claimed.champion else 1,
                zone=claimed.zones[winner],
                slot=claimed.slots[winner],
            )
        )
        return Gauntleted(lineup_id=lineup_id, winner=winner, starred=starred, champion=champion)

    def rank(
        self,
        write: sqlite3.Connection,
        lineup_id: str,
        wallpaper_id: str,
        *,
        ranked: bool,
        theme: Theme | None = None,
    ) -> Lineup | Ordered | LineupRefused:
        """Rank one tile of the live **Line-up** next on its **Order**, or take its rank back with
        `ranked=False`, inside the caller's write transaction; or say why not, before anything is written.

        Taking a rank back closes the ranks after it up, so the ranks are always 1 to k. Sets rather than
        toggles, as a star does: ranking a ranked tile, or taking back an unranked one, changes nothing. Not
        an answer, and answered with the **Line-up** as it now stands, until all but the last tile are
        ranked: that answers it at once, the last place implied, as `order` does, with the page's `theme`.
        """
        claimed = _claim(write, lineup_id)
        if claimed is None:
            return LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
        if wallpaper_id not in {w.id for w in claimed.wallpapers}:
            return LineupRefused(reason=LineupRefused.Reason.NOT_IN_LINEUP)
        if not ranked:
            now_ranked = tuple(w for w in claimed.ranked if w != wallpaper_id)
        elif wallpaper_id in claimed.ranked:
            now_ranked = claimed.ranked
        else:
            now_ranked = (*claimed.ranked, wallpaper_id)
        held = replace(claimed, ranked=now_ranked)
        if len(held.ranked) == len(held.wallpapers) - 1:
            return self._answer_order(write, held, theme=theme)
        write.execute("DELETE FROM lineup_ranks WHERE lineup_id = ?", (lineup_id,))
        write.executemany(
            "INSERT INTO lineup_ranks (lineup_id, wallpaper_id, rank) VALUES (?, ?, ?)",
            [(lineup_id, w, rank) for rank, w in enumerate(held.ranked, start=1)],
        )
        return held

    def order(
        self, write: sqlite3.Connection, lineup_id: str, *, theme: Theme | None = None
    ) -> Ordered | LineupRefused:
        """Answer the live **Line-up** `lineup_id` with the **Order** held against it as it stands, partial or
        empty, inside the caller's write transaction, and drop it; or say why not, before anything is
        written.

        One **Order** version 1 **Judgement** on the compare **Screen**, recorded as a **Pick** is: every
        tile in on-screen order with its rank, none for a tile left unranked, and each star as a
        **Favourite**. A starred tile leaves the **Pool**; nothing else does.
        """
        claimed = _claim(write, lineup_id)
        if claimed is None:
            return LineupRefused(reason=LineupRefused.Reason.UNKNOWN_LINEUP)
        return self._answer_order(write, claimed, theme=theme)

    def _answer_order(self, write: sqlite3.Connection, claimed: Lineup, *, theme: Theme | None) -> Ordered:
        """Record `claimed` with its ranks, the last place filled in where only it is left."""
        unranked = [w.id for w in claimed.wallpapers if w.id not in claimed.ranked]
        complete = replace(claimed, ranked=(*claimed.ranked, *unranked)) if len(unranked) == 1 else claimed
        starred = self._answer(
            write,
            complete,
            ORDER_V1,
            lambda w: OrderTile(rank=complete.rank_of(w), favourite=w in complete.starred),
            None,
            theme=theme,
        )
        return Ordered(lineup_id=complete.id, ranked=complete.ranked, starred=starred)

    def _answer(
        self,
        write: sqlite3.Connection,
        claimed: Lineup,
        instrument: Instrument,
        tile: Callable[[str], ItemResponse],
        response: JudgementResponse | None,
        *,
        theme: Theme | None,
    ) -> tuple[str, ...]:
        """Record one **Judgement** of `claimed` by `instrument`, each tile's **Response** from `tile`, retire
        its starred tiles from the **Pool** and drop it; answer with the starred, in on-screen order.

        Every tile in on-screen order with its **Provenance**, the **Ranking stamp** it was drawn under, its
        mint time as first shown, the clock's local offset, and the page's `theme` in the **Context** where
        it sent one: what every **Instrument** on a **Line-up** records alike.
        """
        shown = [w.id for w in claimed.wallpapers]
        starred = tuple(w for w in shown if w in claimed.starred)
        now = self._clock.now()
        evidence_log.append(
            write,
            Judgement(
                instrument=instrument,
                screen=evidence_log.SCREEN_COMPARE,
                shown=tuple(Shown(evidence_log.item_key(w), tile(w), _provenance(claimed, w)) for w in shown),
                recorded_at=evidence_log.recorded_time(write, shown, now),
                app=evidence_log.APP,
                app_version=evidence_log.APP_VERSION,
                response=response,
                utc_offset=self._clock.utc_offset(now),
                first_shown_at=claimed.created_at,
                ranking=claimed.ranking,
                context=evidence_log.context(theme=theme),
            ),
        )
        pool.retire(write, starred)
        _drop(write, "?", (claimed.id,))
        return starred

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

    def _drawn(
        self, connection: sqlite3.Connection, mix: Mix, candidates: Sequence[ScoredWallpaper], size: int
    ) -> list[Drawn]:
        """`size` tiles from `candidates` by `mix` and their wear: the draw every **Instrument** shares (ADR
        0028). An **Instrument**'s own rule narrows what it hands this, never how it draws: **Gauntlet**'s
        champion stays, so `_mint` hands it the candidates less the champion and asks for one fewer."""
        vectors = self._embeddings.vectors([scored.wallpaper for scored in candidates])
        return draw(mix, candidates, size, self._random, vectors, wear=wear(connection, candidates))


def _keepable(
    keeping: Champion | None, candidates: Sequence[ScoredWallpaper]
) -> tuple[Champion, ScoredWallpaper] | None:
    """The champion and its candidate, if it is one: what could be drawn now is what may be kept."""
    if keeping is None:
        return None
    for scored in candidates:
        if scored.wallpaper.id == keeping.wallpaper_id:
            return keeping, scored
    return None


def wear(connection: sqlite3.Connection, candidates: Sequence[ScoredWallpaper]) -> Wear:
    """How long each candidate should wait before it is lined up again: today, the times it was compared,
    from one **Evidence log** query (ADR 0028). Each showing makes it wait longer; a **Verdict** is what ends
    its turns. The one place wear is worked out: a rule that shows the comparison that teaches the most,
    reading each **Score** and how sure it is, replaces this body and not the draw."""
    return evidence_log.times_compared(connection, [scored.wallpaper.id for scored in candidates])


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
    leaves the **Pool**, and the star is recorded only if the **Line-up** is answered before a visit
    replaces it.
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
    """Drop the live **Line-up** with its stars inside the caller's transaction, and say how many were
    dropped: a **Pool reset**'s share (ADR 0025), and what a visit replaces (ADR 0029). Housekeeping, not an
    answer: nothing is recorded."""
    return _drop(write, "SELECT id FROM lineups")


def _claim(write: sqlite3.Connection, lineup_id: str) -> Lineup | None:
    row = write.execute(
        "SELECT id, created_at, mix, ranking FROM lineups WHERE id = ?", (lineup_id,)
    ).fetchone()
    return None if row is None else _lineup_from(write, row)


def _drop(write: sqlite3.Connection, selection: str, parameters: Sequence[str] = ()) -> int:
    """Delete the **Line-ups** whose ids the SQL `selection` gives (a subquery, or `?` for one), and say how
    many. Stars and ranks, then tiles, then the **Line-up**, for the foreign keys."""
    write.execute(f"DELETE FROM lineup_stars WHERE lineup_id IN ({selection})", parameters)
    write.execute(f"DELETE FROM lineup_ranks WHERE lineup_id IN ({selection})", parameters)
    write.execute(f"DELETE FROM lineup_wallpapers WHERE lineup_id IN ({selection})", parameters)
    return write.execute(f"DELETE FROM lineups WHERE id IN ({selection})", parameters).rowcount


def _lineup_from(connection: sqlite3.Connection, lineup: sqlite3.Row) -> Lineup:
    """A `lineups` row with its tiles, their stars and their ranks."""
    lineup_id = str(lineup["id"])
    rows = connection.execute(_SELECT_LINEUP_WALLPAPERS, (lineup_id,)).fetchall()
    crowned = [row for row in rows if row["streak"] is not None]
    stars = connection.execute("SELECT wallpaper_id FROM lineup_stars WHERE lineup_id = ?", (lineup_id,))
    ranks = connection.execute(
        "SELECT wallpaper_id FROM lineup_ranks WHERE lineup_id = ? ORDER BY rank", (lineup_id,)
    )
    return Lineup(
        id=lineup_id,
        created_at=dt.datetime.fromisoformat(str(lineup["created_at"])),
        wallpapers=tuple(wallpaper_from_row(row) for row in rows),
        zones={str(row["id"]): Zone(str(row["zone"])) for row in rows},
        slots={str(row["id"]): Zone(str(row["slot_zone"])) for row in rows},
        mix=str(lineup["mix"]),
        ranking=None if lineup["ranking"] is None else evidence_log.stamp_from_text(lineup["ranking"]),
        starred=frozenset(str(row["wallpaper_id"]) for row in stars),
        ranked=tuple(str(row["wallpaper_id"]) for row in ranks),
        champion=str(crowned[0]["id"]) if crowned else None,
        streak=int(crowned[0]["streak"]) if crowned else 0,
    )


def _provenance(lineup: Lineup, wallpaper_id: str) -> Provenance:
    """Why a tile was on the **Line-up**: the **Zone** it was drawn from, its **Slot**'s and the **Mix**, and
    whether it was kept as a **Gauntlet**'s champion rather than drawn."""
    return evidence_log.provenance(
        zone=lineup.zones[wallpaper_id],
        slot_zone=lineup.slots[wallpaper_id],
        mix=lineup.mix,
        champion=wallpaper_id == lineup.champion,
    )


_SELECT_LINEUP_WALLPAPERS = """
SELECT w.*, lw.zone AS zone, lw.slot_zone AS slot_zone, lw.streak AS streak
FROM lineup_wallpapers AS lw
JOIN wallpapers AS w ON w.id = lw.wallpaper_id
WHERE lw.lineup_id = ?
ORDER BY lw.position
"""
