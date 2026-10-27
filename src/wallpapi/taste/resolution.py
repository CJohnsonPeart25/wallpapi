"""**Verdict resolution**: the latest-wins rule, written once, and the example cases it is known by.

The latest recorded time decides, whatever it says, and nothing before it counts; an exact tie in recorded
time falls to the lower **Judgement** id, compared as plain strings. Only absolute Evidence is resolved:
relative Evidence is never resolved, every comparison counting, contradictions included.

The **Evidence log**'s SQL is a second spelling of this rule, the one **History** pages with, and is held to
it by loading `EXAMPLE_CASES` and checking it reaches the same `standing`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from wallpapi.taste.evidence import AbsoluteEvidence, Evidence
from wallpapi.taste.verdict_grid import LegacyEntry


def resolve(evidence: Iterable[Evidence]) -> dict[str, AbsoluteEvidence]:
    """The absolute Evidence that stands for each **Item** it names, keyed in the order the Evidence first
    names them. A **Clearance** last stands at 0: it wins, and adds nothing."""
    standing: dict[str, AbsoluteEvidence] = {}
    for piece in evidence:
        if isinstance(piece, AbsoluteEvidence):
            held = standing.get(piece.item)
            if held is None or _decides_over(piece, held):
                standing[piece.item] = piece
    return standing


def _decides_over(piece: AbsoluteEvidence, held: AbsoluteEvidence) -> bool:
    """Latest-wins: the later recorded time decides, and an exact tie falls to the lower **Judgement** id."""
    if piece.recorded_at != held.recorded_at:
        return piece.recorded_at > held.recorded_at
    return piece.judgement_id < held.judgement_id


@dataclass(frozen=True, slots=True)
class Said:
    """One **Item**'s part in one **Judgement**, as a case states it: what was recorded, in the converted
    grid's words, and when."""

    item: str
    recorded_at: dt.datetime
    """UTC-aware, to the microsecond."""
    judgement_id: str
    value: LegacyEntry
    """A plain `str` too: `favourite`, `like`, `ignore`, `ban` or `cleared`."""


@dataclass(frozen=True, slots=True)
class Standing:
    """What an **Item** resolves to: the **Judgement** that decides it, and what that one said."""

    judgement_id: str
    value: LegacyEntry


@dataclass(frozen=True, slots=True)
class ExampleCase:
    """Everything recorded about a few **Items**, and what each must resolve to."""

    name: str
    said: tuple[Said, ...]
    standing: Mapping[str, Standing]


def _at(second: int, microsecond: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, 27, 9, 0, second, microsecond, tzinfo=dt.UTC)


_A = "wallhaven:aaaaaa"
_B = "wallhaven:bbbbbb"

EXAMPLE_CASES: Final[tuple[ExampleCase, ...]] = (
    ExampleCase(
        name="judged once",
        said=(Said(_A, _at(0), "j-1", LegacyEntry.LIKE),),
        standing={_A: Standing("j-1", LegacyEntry.LIKE)},
    ),
    ExampleCase(
        name="a favourite overturned by a later ban",
        said=(
            Said(_A, _at(0), "j-1", LegacyEntry.FAVOURITE),
            Said(_A, _at(5), "j-2", LegacyEntry.BAN),
        ),
        standing={_A: Standing("j-2", LegacyEntry.BAN)},
    ),
    ExampleCase(
        name="a clearance last",
        said=(
            Said(_A, _at(0), "j-1", LegacyEntry.FAVOURITE),
            Said(_A, _at(5), "j-2", LegacyEntry.CLEARED),
        ),
        standing={_A: Standing("j-2", LegacyEntry.CLEARED)},
    ),
    ExampleCase(
        name="a verdict after a clearance",
        said=(
            Said(_A, _at(0), "j-1", LegacyEntry.CLEARED),
            Said(_A, _at(5), "j-2", LegacyEntry.LIKE),
        ),
        standing={_A: Standing("j-2", LegacyEntry.LIKE)},
    ),
    ExampleCase(
        name="a same-time tie",
        said=(
            Said(_A, _at(0), "j-2", LegacyEntry.BAN),
            Said(_A, _at(0), "j-1", LegacyEntry.LIKE),
        ),
        standing={_A: Standing("j-1", LegacyEntry.LIKE)},
    ),
    ExampleCase(
        name="an ignore after a like overturns it",
        said=(
            Said(_A, _at(0), "j-1", LegacyEntry.LIKE),
            Said(_A, _at(5), "j-2", LegacyEntry.IGNORE),
        ),
        standing={_A: Standing("j-2", LegacyEntry.IGNORE)},
    ),
    ExampleCase(
        name="a like after an ignore overturns it",
        said=(
            Said(_A, _at(0), "j-1", LegacyEntry.IGNORE),
            Said(_A, _at(5), "j-2", LegacyEntry.LIKE),
        ),
        standing={_A: Standing("j-2", LegacyEntry.LIKE)},
    ),
    ExampleCase(
        name="one microsecond later decides",
        said=(
            Said(_A, _at(5, 1), "j-1", LegacyEntry.BAN),
            Said(_A, _at(5, 0), "j-2", LegacyEntry.FAVOURITE),
        ),
        standing={_A: Standing("j-1", LegacyEntry.BAN)},
    ),
    ExampleCase(
        name="the latest decides, not the lowest id",
        said=(
            Said(_A, _at(0), "j-2", LegacyEntry.LIKE),
            Said(_A, _at(5), "j-1", LegacyEntry.IGNORE),
        ),
        standing={_A: Standing("j-1", LegacyEntry.IGNORE)},
    ),
    ExampleCase(
        name="a history edit of one item of a batch",
        said=(
            Said(_A, _at(0), "j-batch", LegacyEntry.IGNORE),
            Said(_B, _at(0), "j-batch", LegacyEntry.LIKE),
            Said(_A, _at(5), "j-edit", LegacyEntry.FAVOURITE),
        ),
        standing={
            _A: Standing("j-edit", LegacyEntry.FAVOURITE),
            _B: Standing("j-batch", LegacyEntry.LIKE),
        },
    ),
)
"""The rule's example cases, each about one or two **Items**. Times are UTC to the microsecond; ids are
plain strings, ordered as strings are."""
