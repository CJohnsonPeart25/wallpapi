"""The domain types the Core service, storage and the web layer all share.

The vocabulary is `CONTEXT.md`'s and nothing else: a **Wallpaper**, the four **Verdicts**, and the
**Decision log** entries those **Verdicts** become.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Wallpaper:
    """One Wallhaven image, shaped like a row of its search endpoint's response.

    Frozen because a **Wallpaper** is a value: two with the same fields are the same **Wallpaper**, and
    nothing downstream may mutate one it was handed.
    """

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
    """The four judgements. `StrEnum` so the stored value is the glossary term, not an opaque integer."""

    FAVOURITE = "favourite"
    LIKE = "like"
    IGNORE = "ignore"
    BAN = "ban"


@dataclass(frozen=True, slots=True)
class DecisionEntry:
    """One appended row of the **Decision log**.

    `seq` is the autoincrement the log is ordered and resolved by; `recorded_at` is display-only, because
    every entry from one submit transaction shares it (invariant 4).
    """

    seq: int
    wallpaper_id: str
    batch_id: str | None
    verdict: Verdict
    recorded_at: dt.datetime
