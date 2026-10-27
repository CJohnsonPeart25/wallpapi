"""The **Verdict grid**: the **Instrument** that asks for a **Verdict** or a **Favourite** per tile.

Version 0 is the converted **Decision log**: one entry per tile in the old words, a legacy **Clearance**
among them. Version 1 is the grid as built: a **Verdict** (or none), a **Favourite** flag and a
preview-opened flag per tile, and whether the screen was submitted or passed over.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from wallpapi.taste.judgement import Instrument, ItemResponse, JudgementResponse

VERDICT_GRID_V0: Final = Instrument("verdict grid", 0)
"""The converted **Decision log**: one `LegacyTileResponse` per tile and no judgement-level **Response**."""

VERDICT_GRID_V1: Final = Instrument("verdict grid", 1)
"""The grid as built: one `TileResponse` per tile and a `GridResponse` for the screen."""


class Verdict(StrEnum):
    """What you say about one **Item** on the grid or in **History**: three, since a **Favourite** is an
    act on any tile rather than a **Verdict**."""

    LIKE = "like"
    IGNORE = "ignore"
    BAN = "ban"


class LegacyEntry(StrEnum):
    """One converted **Decision log** entry, in the words the old table stored: the four it called
    verdicts, **Favourite** among them, and a legacy **Clearance**."""

    FAVOURITE = "favourite"
    LIKE = "like"
    IGNORE = "ignore"
    BAN = "ban"
    CLEARED = "cleared"


FAVOURITE_STRENGTH: Final = 100.0
"""A **Favourite**, on any tile of any **Instrument**: the strongest positive."""

VERDICT_STRENGTHS: Final[Mapping[Verdict, float]] = {
    Verdict.LIKE: 50.0,
    Verdict.IGNORE: -10.0,
    Verdict.BAN: -100.0,
}

LEGACY_STRENGTHS: Final[Mapping[LegacyEntry, float]] = {
    LegacyEntry.FAVOURITE: FAVOURITE_STRENGTH,
    **{LegacyEntry(verdict.value): strength for verdict, strength in VERDICT_STRENGTHS.items()},
    LegacyEntry.CLEARED: 0.0,
}
"""Version 0's values: today's four, and 0 for a **Clearance**, which wins as latest and adds nothing."""


@dataclass(frozen=True, slots=True)
class LegacyTileResponse(ItemResponse):
    """Version 0's per-tile **Response**: the converted entry, every tile having one."""

    entry: LegacyEntry


@dataclass(frozen=True, slots=True)
class TileResponse(ItemResponse):
    """Version 1's per-tile **Response**. Blank (`TileResponse()`) is an unmarked tile.

    A **Favourite** and a **Verdict** on one tile are refused with `ValueError`: on the grid they are one
    choice, so one tile never says both 100 and -100. On the grid an **Ignore** is never marked, only left
    unmarked; from **History** it is chosen.
    """

    verdict: Verdict | None = None
    favourite: bool = False
    preview_opened: bool = False

    def __post_init__(self) -> None:
        if self.favourite and self.verdict is not None:
            raise ValueError(f"a tile is a Favourite or a {self.verdict.value}, never both")


@dataclass(frozen=True, slots=True)
class GridResponse(JudgementResponse):
    """Version 1's judgement-level **Response**: whether the screen was submitted. Blank (`GridResponse()`,
    not submitted) is a **Skip**, so a **Skip** is a **Judgement** whose every **Response** is blank, while a
    submitted **Batch** with nothing marked is told apart from it here. A **History** edit is submitted."""

    submitted: bool = False
