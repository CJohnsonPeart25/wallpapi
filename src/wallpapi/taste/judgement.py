"""The **Judgement**: one screen answered, recorded whole. `taste/` defines the shape and stores nothing;
wallpapi keeps it in the **Evidence log**, one row per **Judgement** and one per shown **Item**.

A **Judgement** is identified by a hash of the act itself: what was shown (the **Instrument**, the
**Screen**, the **Shown set** in order), what was done (the **Response**, per **Item** and for the whole
screen), and when (the recorded time, UTC, to the microsecond). Everything recorded *about* the act stays
out of the id: **Provenance**, the **Ranking stamp**, the context, the first-shown time, the local offset and
the app. A converted **Judgement** has none of those, so two copies of one log convert to the same ids
whichever app version converts them; and a later merge recognises one act stamped two ways as one
**Judgement**, which matters because relative Evidence is never resolved and a duplicate would count twice.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

FORMAT_VERSION: Final = 1
"""The format of a **Judgement** and of its canonical form. Every stored shape carries it."""

type Provenance = Mapping[str, str]
"""Why an **Item** was in the **Shown set**: named labels `taste/` keeps and never interprets. wallpapi's are
the **Zone** drawn from, the **Zone** the **Slot** was meant for, and the **Mix**."""

type ContextValue = str | int | float | bool

type Context = Mapping[str, ContextValue]
"""Named fields about the moment, open-ended: the page's theme first. Absent means unknown, and a name this
version does not know is kept as it came."""


@dataclass(frozen=True, slots=True)
class Instrument:
    """One way of asking for a **Judgement**. Its version is the format of its **Response**, and the pair is
    the key of its **Reduction**."""

    name: str
    version: int


@dataclass(frozen=True, slots=True)
class ItemResponse:
    """What was done about one shown **Item**: each **Instrument** gives it fields, each with a default that
    means nothing was done. Field names and values are part of the canonical form, so renaming one is a new
    **Instrument** version."""


@dataclass(frozen=True, slots=True)
class JudgementResponse:
    """What was done about the screen as a whole, for an **Instrument** that has such fields. As
    `ItemResponse`, each field's default means nothing was done."""


@dataclass(frozen=True, slots=True)
class LogPosition:
    """Where in the **Evidence log** ranking read up to: its newest **Judgement**."""

    recorded_at: dt.datetime
    judgement_id: str


@dataclass(frozen=True, slots=True)
class RankingStamp:
    """The ranking a **Shown set** was drawn under: enough to replay the screen as that day's ranking saw it,
    and never a **Score**. `read` is `None` for an empty log. The distillation and lens places stay empty
    until either exists."""

    read: LogPosition | None
    space: str
    """The **Space**: the CLIP model's id, the colour-and-category fallback, or a blend of the two, named
    by wallpapi."""
    radius: float
    decay: float
    distillation: str | None = None
    lens: str | None = None


@dataclass(frozen=True, slots=True)
class Shown:
    """One **Item** of a **Shown set**: its key, what was done about it, and why it was there."""

    item: str
    """Namespaced by **Item source** (`wallhaven:<id>`); `taste/` never looks inside one."""
    response: ItemResponse
    provenance: Provenance = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class Judgement:
    """One screen answered: what was shown, how it was asked, what was done, and when.

    `id` is derived, never given: a hash of the canonical form (`canonical`), so a **Judgement** read back
    from storage carries the id its content earns. A naive time, an empty **Shown set**, an **Item** shown
    twice and another format version are refused with `ValueError`.
    """

    instrument: Instrument
    screen: str
    """Where it was asked, named by the app: `batch`, `history`, `compare`."""
    shown: tuple[Shown, ...]
    """In on-screen order."""
    recorded_at: dt.datetime
    """Submission. Kept in UTC, to the microsecond."""
    app: str
    app_version: str
    response: JudgementResponse | None = None
    utc_offset: dt.timedelta | None = None
    """The local offset from UTC at the recorded time."""
    first_shown_at: dt.datetime | None = None
    """When the screen was first shown: its mint time."""
    ranking: RankingStamp | None = None
    """Absent when nothing ranked the **Shown set**."""
    context: Context = field(default_factory=dict[str, ContextValue])
    format_version: int = FORMAT_VERSION
    id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.format_version != FORMAT_VERSION:
            raise ValueError(
                f"this taste/ reads Judgement format {FORMAT_VERSION}, not {self.format_version}"
            )
        if self.recorded_at.tzinfo is None:
            raise ValueError("a recorded time must be UTC-aware")
        if self.first_shown_at is not None and self.first_shown_at.tzinfo is None:
            raise ValueError("a first-shown time must be UTC-aware")
        if not self.shown:
            raise ValueError("a Judgement's shown set holds at least one Item")
        if len(set(self.shown_set)) != len(self.shown):
            raise ValueError("a shown set never holds an Item twice")
        object.__setattr__(self, "recorded_at", self.recorded_at.astimezone(dt.UTC))
        object.__setattr__(self, "id", hashlib.sha256(canonical(self).encode("ascii")).hexdigest())

    @property
    def shown_set(self) -> tuple[str, ...]:
        """The **Item** keys, in on-screen order."""
        return tuple(shown.item for shown in self.shown)


def canonical(judgement: Judgement) -> str:
    """The text a **Judgement**'s id hashes: compact JSON, keys sorted, ASCII only, holding the format
    version, the **Instrument**, the **Screen**, the recorded time (`instant`), and each shown **Item** with
    its **Response** in order, then the judgement-level **Response** (`null` where there is none). Each
    **Response** is its fields by name, an enum member as its value."""
    content = {
        "format": judgement.format_version,
        "instrument": {"name": judgement.instrument.name, "version": judgement.instrument.version},
        "screen": judgement.screen,
        "recorded_at": instant(judgement.recorded_at),
        "shown": [{"item": shown.item, "response": _fields(shown.response)} for shown in judgement.shown],
        "response": None if judgement.response is None else _fields(judgement.response),
    }
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def instant(at: dt.datetime) -> str:
    """A moment as fixed text: ISO 8601 in UTC with all six microsecond digits, so text order is time order.
    A naive moment could be any zone, so it is refused."""
    if at.tzinfo is None:
        raise ValueError("a moment must be UTC-aware")
    return at.astimezone(dt.UTC).isoformat(timespec="microseconds")


def _fields(response: ItemResponse | JudgementResponse) -> dict[str, object]:
    return {each.name: _plain(getattr(response, each.name)) for each in dataclasses.fields(response)}


def _plain(value: object) -> object:
    return value.value if isinstance(value, Enum) else value
