"""**Batches**: drawing a **Batch** from the **Pool**, its **Draft Batch**, and submitting it into the
**Evidence log** as one **Judgement**. Nothing else reads or writes `batches`, `batch_wallpapers` or
`draft_batch`.

A **Batch** persists until it is submitted (ADR 0002), skipped (ADR 0026) or discarded by a **Pool reset**
(ADR 0025); only submitting records anything. Tile posts set a **Draft Batch** row and never toggle; absence
is an unmarked tile, which a submitted **Judgement** reads as an **Ignore**, and an **Ignore** is never
drafted. A **Batch** is submitted once. Every write takes the caller's write handle, behind one claim check.
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray

from wallpapi import evidence_log, pool, settings, storage
from wallpapi.allocation import PERCENT, ScoredWallpaper, draw
from wallpapi.model import Mix, Verdict, Wallpaper, Zone
from wallpapi.pool import RefillStatus, wallpaper_from_row
from wallpapi.similarity import Embeddings
from wallpapi.taste.clock import Clock, iso_utc
from wallpapi.taste.evidence import AbsoluteEvidence, Evidence
from wallpapi.taste.judgement import Judgement, Provenance, Shown
from wallpapi.taste.ranking import rank
from wallpapi.taste.reductions import evidence
from wallpapi.taste.rng import SeededRandom
from wallpapi.taste.verdict_grid import VERDICT_GRID_V1, GridResponse


@dataclass(frozen=True, slots=True)
class UnknownShort:
    """The **Unknown** zone held fewer **Wallpapers** than the active **Mix** can ask of it, in a **Pool**
    that could fill a **Batch**: the **Similarity radius** has covered the space (ADR 0023).
    """

    unknowns: int
    slots: int
    """The most **Unknown** slots the **Mix** can be allocated: its share of the **Batch**, rounded up."""
    mix: str


@dataclass(frozen=True, slots=True)
class Batch:
    """The **Wallpapers** shown at once, with the identity the submission quotes back.

    `drafts` holds only marked **Wallpapers**: absence is an unmarked tile. `zones` is the **Zone** each was
    drawn from at mint time, never recomputed; absent for a **Batch** minted before **Zones** existed. `mix`
    is the name of the **Mix** it was drawn with, `None` for a **Batch** minted before migration 12.
    `unknown_short` is set only on the **Batch** as minted, from the classification the mint computed, and is
    never stored: a reload has nothing to say (ADR 0023).
    """

    id: str
    size: int
    created_at: dt.datetime
    wallpapers: tuple[Wallpaper, ...]
    drafts: Mapping[str, Verdict]
    zones: Mapping[str, Zone]
    mix: str | None = None
    unknown_short: UnknownShort | None = field(default=None, compare=False)


@dataclass(frozen=True)
class BatchUnavailable:
    """No **Batch** could be built: the **Pool** is empty, and the page needs to say whether that is waiting
    or Wallhaven being unreachable.
    """

    class Reason(StrEnum):
        POOL_EMPTY = "pool_empty"
        """The **Pool** holds nothing but **Bans** and their near-duplicates, and the refill has not
        failed.
        """

        WALLHAVEN_UNREACHABLE = "wallhaven_unreachable"
        """The **Pool** is empty and the last refill attempt failed. `error` says how."""

    reason: Reason
    error: str | None = None
    error_at: dt.datetime | None = None


@dataclass(frozen=True)
class SubmissionRefused:
    """The draft, submission or **Skip** did not happen, and this is why. Never a silent no-op — two tabs is a
    real case.
    """

    class Reason(StrEnum):
        UNKNOWN_BATCH = "unknown_batch"
        ALREADY_SUBMITTED = "already_submitted"
        NOT_IN_BATCH = "not_in_batch"
        """A draft naming a **Wallpaper** the **Batch** does not show: no control posts it."""
        IGNORE_DRAFTED = "ignore_drafted"
        """An **Ignore** is an unmarked tile on a submitted **Batch**, never drafted."""

    reason: Reason


@dataclass(frozen=True, slots=True)
class Submitted:
    """What a submission recorded: one **Judgement** of `recorded` tiles, `ignored` of them left unmarked."""

    batch_id: str
    recorded: int
    ignored: int


def _new_id() -> str:
    return uuid4().hex


class Batches:
    """Mints and submits **Batches**: the parts that need the **Similarity provider**, the Refill's status,
    the clock, the draw's own random source or the id source.
    """

    def __init__(
        self,
        embeddings: Embeddings,
        refill_status: Callable[[], RefillStatus],
        clock: Clock,
        random_source: SeededRandom,
        *,
        new_id: Callable[[], str] = _new_id,
    ) -> None:
        self._embeddings = embeddings
        self._refill_status = refill_status
        self._clock = clock
        self._random = random_source
        self._new_id = new_id

    def next(self, connection: sqlite3.Connection) -> Batch | BatchUnavailable:
        """The **Batch** waiting to be decided on, drawing from the **Pool** only if there isn't one. No **API
        call**.

        Classified outside the write transaction, which stays short. The **Zone** recorded per **Wallpaper**
        is where it was drawn from, never the slot's: after a **Shortfall** the two differ.

        Pre-marking: a chosen **Wallpaper** whose resolved **Verdict** is explicit gets a **Draft Batch** row,
        so leaving it alone records it again (ADR 0015). Dormant while nothing decided is in the **Pool** (ADR
        0016).
        """
        existing = live(connection)
        if existing is not None:
            return existing

        classified = self.classify(connection)
        if not classified:
            return self._nothing_to_show()
        # From the one sequence, so a row of `vectors` cannot drift from its **Wallpaper**.
        vectors = self._embeddings.vectors([scored.wallpaper for scored in classified])
        size = settings.get(connection).batch_size
        mix = settings.active_mix(connection)
        chosen = draw(mix, classified, size, self._random, vectors)
        created_at = self._clock.now()
        batch_id = self._new_id()

        with storage.write(connection) as write:
            # Re-read under the write lock (ADR 0002): two tabs opened at once must not each mint a **Batch**.
            contended = live(write)
            if contended is not None:
                return contended
            write.execute(
                "INSERT INTO batches (id, created_at, size, mix) VALUES (?, ?, ?, ?)",
                (batch_id, iso_utc(created_at), len(chosen), mix.name),
            )
            write.executemany(
                "INSERT INTO batch_wallpapers (batch_id, wallpaper_id, position, zone) VALUES (?, ?, ?, ?)",
                [
                    (batch_id, scored.wallpaper.id, position, scored.zone.value)
                    for position, scored in enumerate(chosen)
                ],
            )
            # Resolved under the write lock, so a **History** edit cannot land between reading a **Verdict**
            # and pre-filling it.
            resolved = evidence_log.resolve(write, [scored.wallpaper.id for scored in chosen])
            explicit = evidence_log.explicitly_decided(write)
            drafts = {
                wallpaper_id: standing.verdict
                for wallpaper_id, standing in resolved.items()
                if standing.verdict is not None and wallpaper_id in explicit
            }
            write.executemany(
                "INSERT INTO draft_batch (batch_id, wallpaper_id, verdict) VALUES (?, ?, ?)",
                [(batch_id, wallpaper_id, verdict.value) for wallpaper_id, verdict in drafts.items()],
            )

        return Batch(
            id=batch_id,
            size=len(chosen),
            created_at=created_at,
            wallpapers=tuple(scored.wallpaper for scored in chosen),
            drafts=drafts,
            zones={scored.wallpaper.id: scored.zone for scored in chosen},
            mix=mix.name,
            unknown_short=_unknown_short(mix, classified, size),
        )

    def classify(self, connection: sqlite3.Connection) -> tuple[ScoredWallpaper, ...]:
        """Every **Pool** **Wallpaper** that is neither **Banned** nor a near-duplicate of a **Ban**, with its
        **Score** and **Zone**, in one call.

        Ranking (`taste/`) gives the **Scores**, from the Evidence `taste/`'s **Reductions** make of every
        **Judgement** in the **Evidence log**; its decided columns are every **Item** standing at a non-zero
        value, in the **Pool** or not. A **Ban** is a column like any other, but never a row. Nor is its
        repost (ADR 0021), which stays in the **Pool**: a **History** edit away from **Ban** brings it back at
        the next call.
        """
        members = pool.members(connection)
        if not members:
            return ()
        judged = pool.decided(connection)
        resolved = evidence_log.resolve(connection, [w.id for w in members] + [w.id for w in judged])

        candidates = [w for w in members if resolved[w.id].verdict is not Verdict.BAN]
        banned = [w for w in judged if resolved[w.id].verdict is Verdict.BAN]
        if candidates and banned:
            reposts = self._embeddings.near_duplicates(candidates, banned).any(axis=1)
            candidates = [w for w, repost in zip(candidates, reposts, strict=True) if not repost]
        if not candidates:
            return ()

        # In **Item** key order, so the decided columns come in the order `pool.decided` gives the
        # **Wallpapers** and every **Score**'s sum runs in the order it always has.
        found = sorted(evidence(evidence_log.judgements(connection)), key=_column_order)
        by_key = {evidence_log.item_key(w.id): w for w in [*candidates, *judged]}

        def similarity(rows: Sequence[str], columns: Sequence[str]) -> NDArray[np.float32]:
            return self._embeddings.similarities([by_key[k] for k in rows], [by_key[k] for k in columns])

        current = settings.get(connection)
        ranking = rank(
            found,
            [evidence_log.item_key(w.id) for w in candidates],
            similarity,
            radius=current.similarity_radius,
            decay=current.similarity_decay,
        )
        return tuple(
            ScoredWallpaper(wallpaper=wallpaper, score=score, zone=zone_of(score))
            for wallpaper, score in zip(candidates, ranking.scores, strict=True)
        )

    def submit(self, write: sqlite3.Connection, batch_id: str) -> Submitted | SubmissionRefused:
        """Record the **Batch** in the **Evidence log** as one **Judgement**, inside the caller's write
        transaction.

        Claim the **Batch**; append a submitted verdict grid **Judgement** of every tile in on-screen order,
        each with its mark or unmarked, the **Zone** it was drawn from and the **Mix** as **Provenance**, its
        mint time as first shown and the clock's local offset; retire everything shown from the **Pool** (ADR
        0016); clear the **Draft Batch**. An unmarked tile is recorded as one, and read as an **Ignore**.
        `BEGIN IMMEDIATE` took the write lock before the claim is read, so a second tab cannot split them.
        """
        claimed = _claim(write, batch_id)
        if isinstance(claimed, SubmissionRefused):
            return claimed
        shown = [w.id for w in claimed.wallpapers]
        now = self._clock.now()
        recorded_at = evidence_log.recorded_time(write, shown, now)
        evidence_log.append(
            write,
            Judgement(
                instrument=VERDICT_GRID_V1,
                screen=evidence_log.SCREEN_BATCH,
                shown=tuple(
                    Shown(
                        evidence_log.item_key(w),
                        evidence_log.tile_response(claimed.drafts.get(w)),
                        _provenance(claimed, w),
                    )
                    for w in shown
                ),
                recorded_at=recorded_at,
                app=evidence_log.APP,
                app_version=evidence_log.APP_VERSION,
                response=GridResponse(submitted=True),
                utc_offset=self._clock.utc_offset(now),
                first_shown_at=claimed.created_at,
            ),
        )
        pool.retire(write, shown)
        write.execute("DELETE FROM draft_batch WHERE batch_id = ?", (batch_id,))
        write.execute("UPDATE batches SET submitted_at = ? WHERE id = ?", (iso_utc(recorded_at), batch_id))
        ignored = sum(1 for w in shown if w not in claimed.drafts)
        return Submitted(batch_id=batch_id, recorded=len(shown), ignored=ignored)

    def _nothing_to_show(self) -> BatchUnavailable:
        """Why the **Pool** had nothing. A recorded refill failure outranks "nothing yet"."""
        status = self._refill_status()
        if status.last_error is not None:
            return BatchUnavailable(
                reason=BatchUnavailable.Reason.WALLHAVEN_UNREACHABLE,
                error=status.last_error,
                error_at=status.last_error_at,
            )
        return BatchUnavailable(reason=BatchUnavailable.Reason.POOL_EMPTY)


def _column_order(piece: Evidence) -> str:
    return piece.item if isinstance(piece, AbsoluteEvidence) else piece.winner


def _provenance(batch: Batch, wallpaper_id: str) -> Provenance:
    """Why a tile was on the **Batch**: the **Zone** it was drawn from and the **Mix**, each where known."""
    labels: dict[str, str] = {}
    if wallpaper_id in batch.zones:
        labels[evidence_log.PROVENANCE_ZONE] = batch.zones[wallpaper_id].value
    if batch.mix is not None:
        labels[evidence_log.PROVENANCE_MIX] = batch.mix
    return labels


def zone_of(score: float) -> Zone:
    """The **Zone** a **Score** falls in: nothing but the sign.

    Beyond the radius a weight is exactly zero, so "no decided neighbour" is a **Score** of exactly 0.0 and
    needs no branch. Exact zero, never a tolerance: a **Score** that merely rounds to zero still says which
    way the evidence went.
    """
    if score > 0.0:
        return Zone.BANGER
    if score < 0.0:
        return Zone.DUD
    return Zone.UNKNOWN


def _unknown_short(mix: Mix, classified: Sequence[ScoredWallpaper], size: int) -> UnknownShort | None:
    """Whether the **Unknown** zone could not serve every allocation `mix` can roll for a **Batch** of
    `size`. A **Pool** smaller than a **Batch** is not the radius's doing, so it says nothing.
    """
    slots = math.ceil(mix.percentage(Zone.UNKNOWN) * size / PERCENT)
    unknowns = sum(1 for scored in classified if scored.zone is Zone.UNKNOWN)
    if unknowns >= slots or len(classified) < size:
        return None
    return UnknownShort(unknowns=unknowns, slots=slots, mix=mix.name)


def set_draft(
    write: sqlite3.Connection, batch_id: str, wallpaper_id: str, verdict: Verdict | None
) -> Batch | SubmissionRefused:
    """Mark one tile of the **Draft Batch**, or clear it with `None`, and answer with the **Batch** as it now
    stands.

    Sets rather than toggles, so a replayed click cannot flip the state; clearing deletes the row, because
    absence is an unmarked tile. Every refusal comes before anything is written.
    """
    if verdict is Verdict.IGNORE:
        return SubmissionRefused(reason=SubmissionRefused.Reason.IGNORE_DRAFTED)
    claimed = _claim(write, batch_id)
    if isinstance(claimed, SubmissionRefused):
        return claimed
    if wallpaper_id not in {w.id for w in claimed.wallpapers}:
        return SubmissionRefused(reason=SubmissionRefused.Reason.NOT_IN_BATCH)
    drafts = dict(claimed.drafts)
    if verdict is None:
        write.execute(
            "DELETE FROM draft_batch WHERE batch_id = ? AND wallpaper_id = ?", (batch_id, wallpaper_id)
        )
        drafts.pop(wallpaper_id, None)
    else:
        write.execute(
            "INSERT INTO draft_batch (batch_id, wallpaper_id, verdict) VALUES (?, ?, ?)"
            " ON CONFLICT (batch_id, wallpaper_id) DO UPDATE SET verdict = excluded.verdict",
            (batch_id, wallpaper_id, verdict.value),
        )
        drafts[wallpaper_id] = verdict
    return replace(claimed, drafts=drafts)


def set_all_drafts(
    write: sqlite3.Connection, batch_id: str, verdict: Verdict | None
) -> Batch | SubmissionRefused:
    """Rewrite the whole **Draft Batch** in the caller's transaction: select-all, or select-none with `None`.

    One transaction rather than one post per tile, so nothing can read the **Batch** half-marked. "All" is
    every tile shown, marked or not.
    """
    if verdict is Verdict.IGNORE:
        return SubmissionRefused(reason=SubmissionRefused.Reason.IGNORE_DRAFTED)
    claimed = _claim(write, batch_id)
    if isinstance(claimed, SubmissionRefused):
        return claimed
    # Scoped to this **Batch**. An unscoped delete reads the same on a database holding one **Draft Batch**
    # and is a silent data loss on any that holds two.
    write.execute("DELETE FROM draft_batch WHERE batch_id = ?", (batch_id,))
    if verdict is None:
        return replace(claimed, drafts={})
    write.execute(_MARK_WHOLE_BATCH, (verdict.value, batch_id))
    return replace(claimed, drafts={w.id: verdict for w in claimed.wallpapers})


def showing(connection: sqlite3.Connection) -> set[str]:
    """Every **Wallpaper** in an unsubmitted **Batch**: the live one, plus any a database from before
    ADR 0002 still holds.
    """
    return {str(row["wallpaper_id"]) for row in connection.execute(_SELECT_SHOWING)}


def discard_live(write: sqlite3.Connection) -> int:
    """Drop every unsubmitted **Batch** with its tiles and **Draft Batch** inside the caller's transaction,
    and say how many: a **Pool reset**'s half (ADR 0025). All of them, so nothing is left showing.

    An unsubmitted **Batch** has no **Judgement**, so nothing is recorded or left dangling. A
    submitted one is never touched.
    """
    return _drop(write, _SELECT_UNSUBMITTED)


def skip(write: sqlite3.Connection, batch_id: str) -> SubmissionRefused | None:
    """Drop the live **Batch** `batch_id` with its tiles and **Draft Batch** inside the caller's
    transaction, or say why not: a **Skip** (ADR 0026).

    Nothing is recorded and nothing leaves the **Pool**, so its tiles may be drawn again. Behind the same
    claim as a submission, so a stale tab cannot skip what another has submitted.
    """
    claimed = _claim(write, batch_id)
    if isinstance(claimed, SubmissionRefused):
        return claimed
    _drop(write, "?", (batch_id,))
    return None


def _drop(write: sqlite3.Connection, selection: str, parameters: Sequence[str] = ()) -> int:
    """Delete the **Batches** whose ids the SQL `selection` gives (a subquery, or `?` for one), and say how
    many. Children first, for the foreign keys.
    """
    write.execute(f"DELETE FROM draft_batch WHERE batch_id IN ({selection})", parameters)
    write.execute(f"DELETE FROM batch_wallpapers WHERE batch_id IN ({selection})", parameters)
    return write.execute(f"DELETE FROM batches WHERE id IN ({selection})", parameters).rowcount


def _claim(write: sqlite3.Connection, batch_id: str) -> Batch | SubmissionRefused:
    """The unsubmitted **Batch** `batch_id`, or why it cannot be drafted against, submitted or skipped."""
    row = write.execute(
        "SELECT id, created_at, size, mix, submitted_at FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        return SubmissionRefused(reason=SubmissionRefused.Reason.UNKNOWN_BATCH)
    if row["submitted_at"] is not None:
        return SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)
    return _batch_from(write, row)


def live(connection: sqlite3.Connection) -> Batch | None:
    """The most recent unsubmitted **Batch**, rebuilt from storage, or `None`. Never mints one, unlike
    `Batches.next`.
    """
    row = connection.execute(
        "SELECT id, created_at, size, mix FROM batches WHERE submitted_at IS NULL ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return None if row is None else _batch_from(connection, row)


def _batch_from(connection: sqlite3.Connection, batch: sqlite3.Row) -> Batch:
    """A `batches` row with its tiles and **Draft Batch**."""
    batch_id = str(batch["id"])
    rows = connection.execute(_SELECT_BATCH_WALLPAPERS, (batch_id,)).fetchall()
    drafted = connection.execute(
        "SELECT wallpaper_id, verdict FROM draft_batch WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    return Batch(
        id=batch_id,
        size=int(batch["size"]),
        created_at=dt.datetime.fromisoformat(str(batch["created_at"])),
        wallpapers=tuple(wallpaper_from_row(row) for row in rows),
        drafts={str(row["wallpaper_id"]): Verdict(str(row["verdict"])) for row in drafted},
        # A **Batch** minted before migration 6 has NULL here, and its tiles go unlabelled.
        zones={str(row["id"]): Zone(str(row["zone"])) for row in rows if row["zone"] is not None},
        mix=None if batch["mix"] is None else str(batch["mix"]),
    )


_SELECT_BATCH_WALLPAPERS = """
SELECT w.*, bw.zone AS zone
FROM batch_wallpapers AS bw
JOIN wallpapers AS w ON w.id = bw.wallpaper_id
WHERE bw.batch_id = ?
ORDER BY bw.position
"""

_MARK_WHOLE_BATCH = """
INSERT INTO draft_batch (batch_id, wallpaper_id, verdict)
SELECT batch_id, wallpaper_id, ?
FROM batch_wallpapers
WHERE batch_id = ?
"""
"""Select-all as one statement, read inside the same transaction as the delete."""


_SELECT_SHOWING = """
SELECT bw.wallpaper_id
FROM batch_wallpapers AS bw
JOIN batches AS b ON b.id = bw.batch_id
WHERE b.submitted_at IS NULL
"""
"""Every tile of every unsubmitted **Batch**."""

_SELECT_UNSUBMITTED = "SELECT id FROM batches WHERE submitted_at IS NULL"
"""What `discard_live` drops: the live **Batch**, plus any a database from before ADR 0002 still holds."""
