"""The **Verdict grid**: the **Instrument** that asks for a **Verdict** or a **Favourite** per tile.

Version 0 is the converted **Decision log**: one entry per tile in the old words, a legacy **Clearance**
among them. Version 1 is the grid as built: a **Verdict** (or none), a **Favourite** flag and a
preview-opened flag per tile, and whether the screen was submitted or passed over.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final


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
