"""The domain types the Core service, storage and the web layer all share.

The vocabulary is `CONTEXT.md`'s and nothing else: a **Wallpaper**, the four **Verdicts**, the
**Clearance** that withdraws one, and the **Decision log** entries they become.
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


class Clearance(StrEnum):
    """The **Decision log** entry that removes a **Wallpaper**'s **Explicit Verdict**.

    Deliberately not a fifth **Verdict**. CONTEXT.md is explicit that "a clearance is an entry in its own
    right, not a verdict": nothing about a **Wallpaper** is being judged, a judgement is being withdrawn,
    and after it the **Ignores** stack again exactly as they did before one was given.

    A one-member `StrEnum` rather than a bare `"cleared"` constant so that `Verdict | Clearance` is a
    closed union pyright narrows — every reader of an entry has to say which of the two it is handling —
    and so the stored text is the glossary term, as it is for a **Verdict**.
    """

    CLEARED = "cleared"


@dataclass(frozen=True, slots=True)
class DecisionEntry:
    """One appended row of the **Decision log**.

    `seq` is the autoincrement the log is ordered and resolved by; `recorded_at` is display-only, because
    every entry from one submit transaction shares it (invariant 4).

    `entry` and not `verdict`: the log holds **Clearances** as well as **Verdicts** (#7), and a field named
    for one of the two would let a caller pass a **Clearance** to something that only handles **Verdicts**
    without pyright saying a word.

    `batch_id` is `NULL` for an entry that came from **History** rather than from a submitted **Batch**,
    which is the only place the two are told apart.
    """

    seq: int
    wallpaper_id: str
    batch_id: str | None
    entry: Verdict | Clearance
    recorded_at: dt.datetime


class Zone(StrEnum):
    """Where a **Pool** **Wallpaper**'s **Score** puts it.

    Three and only three, and a **Banned** **Wallpaper** is in none of them — which is why this has no
    fourth member: "**Banned**" is not a **Zone** a **Wallpaper** can be shown from, it is the reason it is
    never classified at all.

    `StrEnum` for the same reason `Verdict` is one: the value recorded against a **Batch** row, and read
    back off it, is the glossary term rather than an opaque integer.
    """

    BANGER = "banger"
    DUD = "dud"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Mix:
    """The **Zone** percentages a **Batch** is built from, under the name the user switches by.

    Frozen, and a value like `Wallpaper` is: two **Mixes** with the same name and the same three numbers
    are the same **Mix**. The three are whole percentages and they sum to 100 — but that rule is *not*
    enforced here. It lives in `core.validated_mix`, because a **Mix** arrives from a stored row and, at
    #12, from a form, and a dataclass that raised would turn "the user typed 30/30/30" into a traceback
    instead of a message next to the field. Nothing constructs one except that validator and the migration
    that seeds these two.

    The three are named rather than a `Mapping[Zone, int]` so that a missing **Zone** is not expressible.
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
