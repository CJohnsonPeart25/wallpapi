"""The domain types the Core service, storage and the web layer share, in `CONTEXT.md`'s vocabulary."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Wallpaper:
    """One Wallhaven image, shaped like a row of its search response."""

    id: str
    width: int
    height: int
    ratio: str
    category: str
    purity: str
    favourites: int
    colours: tuple[str, ...]
    thumbnail_url: str
    full_url: str
    page_url: str


class Verdict(StrEnum):
    """The four judgements. `StrEnum` so the stored value is the glossary term."""

    FAVOURITE = "favourite"
    LIKE = "like"
    IGNORE = "ignore"
    BAN = "ban"


class Clearance(StrEnum):
    """The legacy **Decision log** entry that withdrew an **Explicit Verdict** (ADR 0015); read, never
    written, and it resolves to nothing.
    """

    CLEARED = "cleared"


@dataclass(frozen=True, slots=True)
class DecisionEntry:
    """One appended row of the **Decision log**.

    `seq` orders and resolves the log; `recorded_at` is display-only (invariant 4). `entry` is not named
    `verdict` because the log also holds legacy **Clearances**.
    """

    seq: int
    wallpaper_id: str
    batch_id: str | None
    entry: Verdict | Clearance
    recorded_at: dt.datetime


class Zone(StrEnum):
    """Where a **Pool** **Wallpaper**'s **Score** puts it. A **Banned** one is in none."""

    BANGER = "banger"
    DUD = "dud"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Mix:
    """The **Zone** percentages a **Batch** is built from, under a name the user switches by.

    Does not validate itself: `core.validated_mix` does, because a form must get a refusal, not a traceback.
    """

    name: str
    unknown: int
    banger: int
    dud: int

    def percentage(self, zone: Zone) -> int:
        """This **Mix**'s share of a **Batch** for one **Zone**."""
        if zone is Zone.UNKNOWN:
            return self.unknown
        if zone is Zone.BANGER:
            return self.banger
        return self.dud
