"""**Batches**: drawing a **Batch** from the **Pool**, its **Draft Batch**, and submitting it into the
**Decision log**. Nothing else reads or writes `batches`, `batch_wallpapers` or `draft_batch`.

A **Batch** persists until it is submitted, and there is no skip (ADR 0002). Tile posts set a **Draft Batch**
row and never toggle; absence is an **Ignore**, derived at submit and never drafted. A **Batch** is submitted
once. Every write takes the caller's write handle, behind one claim check.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import uuid4

from wallpapi import decisions, pool, settings, storage
from wallpapi.allocation import ScoredWallpaper, draw
from wallpapi.clock import Clock
from wallpapi.model import Verdict, Wallpaper, Zone
from wallpapi.pool import RefillStatus, wallpaper_from_row
from wallpapi.rng import SeededRandom
from wallpapi.scoring import classify
from wallpapi.similarity import Embeddings


@dataclass(frozen=True, slots=True)
class Batch:
    """The **Wallpapers** shown at once, with the identity the submission quotes back.

    `drafts` holds only marked **Wallpapers**: absence is an **Ignore**. `zones` is the **Zone** each was
    drawn from at mint time, never recomputed; absent for a **Batch** minted before **Zones** existed.
    """

    id: str
    size: int
    created_at: dt.datetime
    wallpapers: tuple[Wallpaper, ...]
    drafts: Mapping[str, Verdict]
    zones: Mapping[str, Zone]


@dataclass(frozen=True)
class BatchUnavailable:
    """No **Batch** could be built: the **Pool** is empty, and the page needs to say whether that is waiting
    or Wallhaven being unreachable.
    """

    class Reason(StrEnum):
        POOL_EMPTY = "pool_empty"
        """The **Pool** holds nothing the user has not **Banned**, and the refill has not failed."""

        WALLHAVEN_UNREACHABLE = "wallhaven_unreachable"
        """The **Pool** is empty and the last refill attempt failed. `error` says how."""

    reason: Reason
    error: str | None = None
    error_at: dt.datetime | None = None


@dataclass(frozen=True)
class SubmissionRefused:
    """The draft or submission did not happen, and this is why. Never a silent no-op — two tabs is a real
    case.
    """

    class Reason(StrEnum):
        UNKNOWN_BATCH = "unknown_batch"
        ALREADY_SUBMITTED = "already_submitted"
        NOT_IN_BATCH = "not_in_batch"
        """A draft naming a **Wallpaper** the **Batch** does not show: no control posts it."""
        IGNORE_DRAFTED = "ignore_drafted"
        """An **Ignore** is derived at submit from an absent row, never drafted."""

    reason: Reason


@dataclass(frozen=True, slots=True)
class Submitted:
    """What a submission appended: one entry per **Wallpaper** shown, `ignored` of them **Ignores**."""

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
        live = _load_live_batch(connection)
        if live is not None:
            return live

        classified = self.classify(connection)
        if not classified:
            return self._nothing_to_show()
        # From the one sequence, so a row of `vectors` cannot drift from its **Wallpaper**.
        vectors = self._embeddings.vectors([scored.wallpaper for scored in classified])
        size = settings.get(connection).batch_size
        chosen = draw(settings.active_mix(connection), classified, size, self._random, vectors)
        created_at = self._clock.now()
        batch_id = self._new_id()

        with storage.write(connection) as write:
            # Re-read under the write lock (ADR 0002): two tabs opened at once must not each mint a **Batch**.
            contended = _load_live_batch(write)
            if contended is not None:
                return contended
            write.execute(
                "INSERT INTO batches (id, created_at, size) VALUES (?, ?, ?)",
                (batch_id, created_at.isoformat(), len(chosen)),
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
            resolved = decisions.resolve(write, [scored.wallpaper.id for scored in chosen])
            explicit = decisions.explicitly_decided(write)
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
        )

    def classify(self, connection: sqlite3.Connection) -> tuple[ScoredWallpaper, ...]:
        """Every **Pool** **Wallpaper** that is not **Banned**, with its **Score** and **Zone**, in one call.

        The decided columns are every **Wallpaper** with a non-zero resolved value, in the **Pool** or not; a
        **Ban** is a column like any other, but never a row.
        """
        members = pool.members(connection)
        if not members:
            return ()
        judged = [wallpaper_from_row(row) for row in connection.execute(_SELECT_DECIDED_WALLPAPERS)]
        resolved = decisions.resolve(connection, [w.id for w in members] + [w.id for w in judged])

        candidates = [w for w in members if resolved[w.id].verdict is not Verdict.BAN]
        decided = [w for w in judged if resolved[w.id].value != 0]
        if not candidates:
            return ()

        current = settings.get(connection)
        classification = classify(
            self._embeddings.similarities(candidates, decided),
            [resolved[w.id].value for w in decided],
            radius=current.similarity_radius,
            decay=current.similarity_decay,
        )
        return tuple(
            ScoredWallpaper(wallpaper=wallpaper, score=float(score), zone=zone)
            for wallpaper, score, zone in zip(
                candidates, classification.scores, classification.zones, strict=True
            )
        )

    def submit(self, write: sqlite3.Connection, batch_id: str) -> Submitted | SubmissionRefused:
        """Append the **Batch**'s **Verdicts** to the **Decision log**, inside the caller's write
        transaction.

        Claim the **Batch**, append the **Explicit Verdicts** and an **Ignore** for every unmarked tile,
        retire everything shown from the **Pool** (ADR 0016), clear the **Draft Batch**. `BEGIN IMMEDIATE`
        took the write lock before the claim is read, so a second tab cannot split them.
        """
        claimed = _claim(write, batch_id)
        if isinstance(claimed, SubmissionRefused):
            return claimed
        recorded_at = self._clock.now()
        entries = {w.id: claimed.drafts.get(w.id, Verdict.IGNORE) for w in claimed.wallpapers}
        decisions.append(write, entries, batch_id=batch_id, at=recorded_at)
        pool.retire(write, list(entries))
        write.execute("DELETE FROM draft_batch WHERE batch_id = ?", (batch_id,))
        write.execute("UPDATE batches SET submitted_at = ? WHERE id = ?", (recorded_at.isoformat(), batch_id))
        ignored = sum(1 for verdict in entries.values() if verdict is Verdict.IGNORE)
        return Submitted(batch_id=batch_id, recorded=len(entries), ignored=ignored)

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


def set_draft(
    write: sqlite3.Connection, batch_id: str, wallpaper_id: str, verdict: Verdict | None
) -> Batch | SubmissionRefused:
    """Mark one tile of the **Draft Batch**, or clear it with `None`, and answer with the **Batch** as it now
    stands.

    Sets rather than toggles, so a replayed click cannot flip the state; clearing deletes the row, because
    absence is an **Ignore**. Every refusal comes before anything is written.
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


def _claim(write: sqlite3.Connection, batch_id: str) -> Batch | SubmissionRefused:
    """The unsubmitted **Batch** `batch_id`, or why it cannot be drafted against or submitted."""
    row = write.execute(
        "SELECT id, created_at, size, submitted_at FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        return SubmissionRefused(reason=SubmissionRefused.Reason.UNKNOWN_BATCH)
    if row["submitted_at"] is not None:
        return SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)
    return _batch_from(write, row)


def _load_live_batch(connection: sqlite3.Connection) -> Batch | None:
    """The most recent unsubmitted **Batch**, rebuilt from storage, or `None`."""
    row = connection.execute(
        "SELECT id, created_at, size FROM batches WHERE submitted_at IS NULL ORDER BY rowid DESC LIMIT 1"
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
    )


_SELECT_DECIDED_WALLPAPERS = f"""
SELECT w.*
FROM wallpapers AS w
WHERE w.id IN ({decisions.MENTIONED})
ORDER BY w.id
"""
"""Every **Wallpaper** the **Decision log** mentions, as whole rows, in a fixed order.

Whole rows because the **Similarity provider** is handed **Wallpapers**; ordered so the matrix's columns, and
so every **Score**, are reproducible.
"""

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
