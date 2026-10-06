"""Everything configurable, as one table of field descriptors, and the **Mixes** beside it (ADR 0004).

Each setting is one `Field` declared on `Settings`: its key, its default, the parser from posted text to a
typed value or a refusal, the words a refusal is said in, and how the form shows it — its `Section`, label,
hint, step and bounds. `get`, `update`, the seeds migrations write and the settings form all iterate that
table, in declaration order, so adding a setting is one descriptor here and no template edit.

Writes take the caller's write handle (a connection already inside `storage.write`) and open no transaction,
so the caller can do its own work in the same one. Reads take any connection.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn, Self, cast, overload

from wallpapi.model import Mix

MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 64
"""The accepted batch size range, inclusive: a **Batch** of none has no way off it, and 64 is as many as
anyone can judge at once.
"""

DEFAULT_BATCH_SIZE = 8

MIN_POOL_TARGET_SIZE = 1
MAX_POOL_TARGET_SIZE = 20_000
"""The accepted **Pool** target size range. Every whole-**Pool** operation is linear in it, so there is a
ceiling rather than a **Pool** that grows and slows for ever.
"""

DEFAULT_POOL_TARGET_SIZE = 500
"""Enough to draw from, not a backlog (ADR 0016): every submission retires what it showed, and smaller is
cheaper on every **Batch** minted.
"""

SUPERSEDED_POOL_TARGET_SIZE = 2000
"""The target ADR 0005 seeded, kept only because migration 10 has to recognise it."""

MAX_FILTER_PIXELS = 30_000
"""The largest minimum resolution accepted, per axis. Anything above it is a typo and an empty **Pool**."""

DEFAULT_MIN_WIDTH = 2560
DEFAULT_MIN_HEIGHT = 1440
"""1440p as a minimum (`atleast`): native 1440p and larger, not upscaled 1080p."""

DEFAULT_ALLOWED_RATIOS = ("16x9", "16x10", "21x9")
"""The shapes a desktop monitor actually is: widescreen, 16:10 and ultrawide."""

WALLHAVEN_RATIOS = frozenset(
    {"16x9", "16x10", "21x9", "32x9", "48x9", "9x16", "10x16", "9x18", "1x1", "3x2", "4x3", "5x4"}
)
"""The `ratios=` values Wallhaven accepts. An unrecognised one is not an error there, just a different
search.
"""

DEFAULT_MIN_FAVOURITES = 10
"""Skips the long tail nobody has looked at. Applied locally: Wallhaven's search has no parameter for it."""

SUPERSEDED_SIMILARITY_RADIUS = 0.5
"""The radius ADR 0007 seeded, kept only because migration 9 has to recognise it."""

RETUNED_SIMILARITY_RADIUS = 0.15
"""The radius ADR 0013 seeded, kept only because migration 11 has to recognise it."""

DEFAULT_SIMILARITY_RADIUS = 0.10
"""How far a decided **Wallpaper**'s influence reaches, as a distance in `[0, 1]`.

Sized to leave an **Unknown** zone, which is what **Explore** draws from. A fixed radius covers more of the
space with every decision, so this decays as the **Decision log** grows: 0.15 left none at 692 decided. See
ADR 0023.
"""

DEFAULT_SIMILARITY_DECAY = 4.0
"""How fast that influence fades, as the rate in `exp(-decay * distance)`. Zero is allowed: no fading."""

MAX_SIMILARITY_DECAY = 50.0
"""`exp(-50 * d)` is under `1e-21` at a hundredth of the range; anything higher is a **Pool** of
**Unknowns**.
"""

DEFAULT_THUMBNAIL_CACHE_MAX_MB = 500
"""The **Thumbnail cache**'s size cap, a backstop behind eviction by **Verdict** (`thumbnails.py`).

At about 23KiB a thumbnail it never fires in normal running. The downloader holds off at the cap rather than
churn against it (ADR 0017).
"""

EXPLORE_MIX = Mix(name="explore", unknown=75, banger=20, dud=5)
REFINE_MIX = Mix(name="refine", unknown=25, banger=70, dud=5)
DEFAULT_MIXES = (EXPLORE_MIX, REFINE_MIX)
"""The two **Mixes** the spec names: the seed for an empty database and the fallback for an emptied table.

The 5% of **Duds** is deliberate: a **Dud** can stop being one the moment something near it is **Favourited**,
and never will if it is never shown.
"""

DEFAULT_ACTIVE_MIX = EXPLORE_MIX.name
"""**Explore**, because an empty **Decision log** has no **Bangers** to refine towards."""

MIX_TOTAL = 100
"""What a **Mix** must sum to, exactly: a **Mix** summing to 99 would make the leftover roll systematic."""

MAX_MIX_NAME_LENGTH = 40
"""How long a **Mix** name may be: it is a button beside the others, and must not push them off the row."""

UNDELETABLE_MIXES = frozenset(mix.name for mix in DEFAULT_MIXES)
"""**Explore** and **Refine**: editable, never deleted, so `CONTEXT.md`'s two terms always have a **Mix**."""


@dataclass(frozen=True)
class SettingsRefused:
    """The update did not happen, and this is why. A result, so the page has one error branch."""

    class Reason(StrEnum):
        NOT_A_NUMBER = "not_a_number"
        OUT_OF_RANGE = "out_of_range"
        EMPTY = "empty_value"
        NOT_ABSOLUTE = "not_absolute"
        NOT_A_WALLHAVEN_RATIO = "not_a_wallhaven_ratio"
        """The ways a field's text is refused, shared by every field; `SettingsRefused.field` says which."""

        ACTIVE_MIX_UNKNOWN = "active_mix_unknown"
        """No stored **Mix** goes by that name."""

        MIX_NAME_INVALID = "mix_name_invalid"
        MIX_PERCENTAGES_INVALID = "mix_percentages_invalid"
        """The two ways `validated_mix` refuses, so the form can say which field is wrong."""

        MIX_UNKNOWN = "mix_unknown"
        """No stored **Mix** goes by that name, so there is nothing to delete."""

        MIX_IN_USE = "mix_in_use"
        """That **Mix** is the active one; switch away first."""

        MIX_NOT_DELETABLE = "mix_not_deletable"
        """**Explore** and **Refine** are editable and permanent."""

    reason: Reason
    field: str | None = None
    """The key of the setting refused, or `None` for a **Mix**."""

    @property
    def message(self) -> str:
        """The refusal in words, as the settings page says it. Never the enum value, unless nothing else
        fits: then the reason is named rather than described as something it is not.
        """
        if self.field is not None:
            return _BY_KEY[self.field].refusal(self.reason)
        return _MIX_REFUSALS.get(self.reason, f"That value was refused ({self.reason.value}).")


type _Reason = SettingsRefused.Reason
type _Parser[T] = Callable[[str], T | SettingsRefused.Reason]


@dataclass(frozen=True, eq=False)
class Section:
    """One group of the settings form: an `<article>`, with its heading and its intro when it has them.

    Equal only to itself, so two sections with no words are still two.
    """

    heading: str | None = None
    intro: str | None = None


# The two settings that came before there were groups, so they have no heading of their own.
_UNHEADED = Section()
_FILTERS = Section(
    heading="Filters",
    intro="The hard rules a wallpaper must pass before it joins the pool. Changing them drops undecided pool "
    "wallpapers that no longer pass; anything you have already judged stays, and so does the batch on "
    "screen. Only safe-for-work wallpapers are ever fetched.",
)
_POOL = Section(heading="Pool")
_SCORING = Section(
    heading="Scoring",
    intro="How far a verdict spreads to wallpapers that look like it. Every score is worked out from the "
    "decision log each time a batch is built, so changing these two takes effect on the next batch and "
    "nothing has to be rebuilt. Both are starting points rather than tuned numbers.",
)
_THUMBNAILS = Section(heading="Thumbnails")


class Field[T]:
    """One setting: everything about it, in one place. Declared on `Settings`, where reading it off an
    instance gives the typed value and reading it off the class gives this descriptor.
    """

    key: str
    """The `settings` row's key, and the form field's name: the attribute name it is declared under."""

    def __init__(
        self,
        *,
        default: T,
        parse: _Parser[T],
        text: Callable[[T], str] = str,
        message: str | Mapping[_Reason, str],
        minimum: float | None = None,
        maximum: float | None = None,
        choices: Sequence[str] = (),
        on_form: bool = True,
        admit: Callable[[sqlite3.Connection, T], _Reason | None] | None = None,
        section: Section | None = None,
        label: str = "",
        help: str = "",
        step: str | None = None,
    ) -> None:
        self.default = default
        self.parse = parse
        """Posted or stored text to a typed value, or the reason it is not one. Never touches anything."""
        self.text = text
        """A typed value as stored, and as the form shows it: `parse(text(value)) == value`."""
        self.message = message
        """The refusal in words: one for every reason, or one per reason. `{minimum}`, `{maximum}` and
        `{choices}` are filled in.
        """
        self.minimum = minimum
        self.maximum = maximum
        self.choices = choices
        """The bounds and choices the form shows: the same numbers `parse` holds the value to."""
        self.on_form = on_form
        """Whether the settings form carries it. The active **Mix** is chosen on the **Batch** page."""
        self.admit = admit
        """A check against the database after parsing, for a setting that names a row elsewhere."""
        self.section = section
        """The group of the form it shows in; a field off the form has none."""
        self.label = label
        self.help = help
        """The hint under the input, filled in like `message`: read it as `hint`."""
        self.step = step
        """The number input's step, as the form writes it. No step is a text input."""

    def __set_name__(self, owner: type, name: str) -> None:
        self.key = name

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Settings, owner: type) -> T: ...
    def __get__(self, instance: Settings | None, owner: type) -> Self | T:
        if instance is None:
            return self
        return cast(T, instance.__dict__[self.key])

    def refusal(self, reason: _Reason) -> str:
        """This field's refusal for that reason, in words."""
        template = self.message if isinstance(self.message, str) else self.message.get(reason)
        if template is None:
            return f"That value was refused ({reason.value})."
        return self._filled(template)

    @property
    def hint(self) -> str:
        """The help under the input, with this field's own bounds and choices in it."""
        return self._filled(self.help)

    def _filled(self, template: str) -> str:
        return template.format(minimum=self.minimum, maximum=self.maximum, choices=", ".join(self.choices))


def _whole(
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
    message: str | Mapping[_Reason, str],
    section: Section,
    label: str,
    help: str,
) -> Field[int]:
    """A whole-number setting held to `[minimum, maximum]`: the bounds the form shows are the rule's, and
    its input steps by one.
    """

    def parse(text: str) -> int | _Reason:
        # "1.5" and "1e3" are refused rather than coerced.
        try:
            number = int(text.strip())
        except ValueError:
            return SettingsRefused.Reason.NOT_A_NUMBER
        if number < minimum or (maximum is not None and number > maximum):
            return SettingsRefused.Reason.OUT_OF_RANGE
        return number

    return Field(
        default=default,
        parse=parse,
        message=message,
        minimum=minimum,
        maximum=maximum,
        section=section,
        label=label,
        help=help,
        step="1",
    )


def _decimal(
    *,
    default: float,
    minimum: float,
    maximum: float,
    message: str,
    section: Section,
    label: str,
    help: str,
    step: str,
) -> Field[float]:
    """A finite decimal setting held to `[minimum, maximum]`. NaN parses, and a NaN radius would make
    everything **Unknown**, so it is not a number here.
    """

    def parse(text: str) -> float | _Reason:
        try:
            number = float(text.strip())
        except ValueError:
            return SettingsRefused.Reason.NOT_A_NUMBER
        if not math.isfinite(number):
            return SettingsRefused.Reason.NOT_A_NUMBER
        if not minimum <= number <= maximum:
            return SettingsRefused.Reason.OUT_OF_RANGE
        return number

    return Field(
        default=default,
        parse=parse,
        message=message,
        minimum=minimum,
        maximum=maximum,
        section=section,
        label=label,
        help=help,
        step=step,
    )


def _absolute_path(text: str) -> Path | _Reason:
    """The **Library** path, absolute, or the reason it is not one. Never touches the filesystem."""
    stripped = text.strip()
    if not stripped:
        return SettingsRefused.Reason.EMPTY
    path = Path(stripped)
    if not path.is_absolute():
        return SettingsRefused.Reason.NOT_ABSOLUTE
    return path


def _wallhaven_ratios(text: str) -> tuple[str, ...] | _Reason:
    """The allowed ratios, trimmed and de-duplicated in order, or the reason they are not.

    A blank list is refused as more likely a mistake than an intention; a blank among others is not a ratio.
    """
    named = tuple(dict.fromkeys(part.strip() for part in text.split(",")))
    if not any(named):
        return SettingsRefused.Reason.EMPTY
    if any(part not in WALLHAVEN_RATIOS for part in named):
        return SettingsRefused.Reason.NOT_A_WALLHAVEN_RATIO
    return named


def _mix_name(text: str) -> str | _Reason:
    """The active **Mix**'s name, trimmed. Which names exist is `_a_stored_mix`'s question."""
    return text.strip() or SettingsRefused.Reason.ACTIVE_MIX_UNKNOWN


def _a_stored_mix(connection: sqlite3.Connection, name: str) -> _Reason | None:
    # Checked inside the write: a second tab deleting this **Mix** later leaves `active_mix` naming nothing,
    # and `active_mix()` falls back.
    if name not in {listed.mix.name for listed in list_mixes(connection)}:
        return SettingsRefused.Reason.ACTIVE_MIX_UNKNOWN
    return None


class Settings:
    """Everything configurable, as one typed view over one row per key. Frozen, and equal by value.

    Each class attribute is a `Field`, in the order the form, `update` and the refusal precedence follow.
    """

    batch_size = _whole(
        default=DEFAULT_BATCH_SIZE,
        minimum=MIN_BATCH_SIZE,
        maximum=MAX_BATCH_SIZE,
        message={
            SettingsRefused.Reason.OUT_OF_RANGE: "Batch size must be between {minimum} and {maximum}.",
            SettingsRefused.Reason.NOT_A_NUMBER: "Batch size must be a whole number between {minimum} and "
            "{maximum}.",
        },
        section=_UNHEADED,
        label="Batch size",
        help="Wallpapers per batch, {minimum} to {maximum}. Applies to the next batch — the one on screen "
        "keeps the size it was built with.",
    )
    library_path = Field(
        # Under Pictures, where Windows' slideshow settings start, in a subfolder of its own.
        default=Path.home() / "Pictures" / "wallpapi",
        parse=_absolute_path,
        message={
            SettingsRefused.Reason.EMPTY: "The library folder cannot be blank.",
            SettingsRefused.Reason.NOT_ABSOLUTE: "The library folder must be an absolute path, such as "
            "C:\\Users\\you\\Pictures\\wallpapi.",
        },
        section=_UNHEADED,
        label="Library folder",
        help="An absolute path. Favourites are downloaded here; the folder is created the first time one is "
        "written.",
    )
    min_width = _whole(
        default=DEFAULT_MIN_WIDTH,
        minimum=0,
        maximum=MAX_FILTER_PIXELS,
        message="Minimum width must be a whole number of pixels from {minimum} to {maximum}.",
        section=_FILTERS,
        label="Minimum width",
        help="Pixels. A minimum, not an exact size — anything larger counts.",
    )
    min_height = _whole(
        default=DEFAULT_MIN_HEIGHT,
        minimum=0,
        maximum=MAX_FILTER_PIXELS,
        message="Minimum height must be a whole number of pixels from {minimum} to {maximum}.",
        section=_FILTERS,
        label="Minimum height",
        help="Pixels. Set either to 0 to stop filtering on that side.",
    )
    allowed_ratios = Field(
        default=DEFAULT_ALLOWED_RATIOS,
        parse=_wallhaven_ratios,
        text=",".join,
        choices=sorted(WALLHAVEN_RATIOS),
        message="Allowed ratios must be a comma-separated list of ratios Wallhaven knows: {choices}.",
        section=_FILTERS,
        label="Allowed ratios",
        help="Comma-separated, from {choices}.",
    )
    # No ceiling: no number of favourites is clearly a typo.
    min_favourites = _whole(
        default=DEFAULT_MIN_FAVOURITES,
        minimum=0,
        message="Minimum favourites must be a whole number, {minimum} or more.",
        section=_FILTERS,
        label="Minimum favourites",
        help="How many Wallhaven favourites a wallpaper needs. Applied here rather than in the search — "
        "Wallhaven has no parameter for it.",
    )
    pool_target_size = _whole(
        default=DEFAULT_POOL_TARGET_SIZE,
        minimum=MIN_POOL_TARGET_SIZE,
        maximum=MAX_POOL_TARGET_SIZE,
        message="Pool target size must be a whole number between {minimum} and {maximum}.",
        section=_POOL,
        label="Pool target size",
        help="How many filtered wallpapers to keep waiting. The background refill fetches at full speed "
        "until the pool reaches this, then idles.",
    )
    # Above 1 the radius would do nothing.
    similarity_radius = _decimal(
        default=DEFAULT_SIMILARITY_RADIUS,
        minimum=0,
        maximum=1,
        message="Similarity radius must be a number from 0 to 1.",
        section=_SCORING,
        label="Similarity radius",
        help="From 0 to 1. How different two wallpapers can be and still tell you anything about each "
        "other — beyond it, a verdict counts for nothing at all.",
        step="0.01",
    )
    # Negative would invert the rule.
    similarity_decay = _decimal(
        default=DEFAULT_SIMILARITY_DECAY,
        minimum=0,
        maximum=MAX_SIMILARITY_DECAY,
        message="Similarity decay must be a number from 0 to {maximum}.",
        section=_SCORING,
        label="Similarity decay",
        help="From 0 to {maximum}. How fast a verdict fades with distance. Higher is fussier; 0 counts "
        "everything inside the radius equally.",
        step="0.1",
    )
    # Zero is accepted: the cap never evicts an **Explicit Verdict**, so **History** still renders.
    thumbnail_cache_max_mb = _whole(
        default=DEFAULT_THUMBNAIL_CACHE_MAX_MB,
        minimum=0,
        message="The thumbnail cache limit must be a whole number of megabytes, {minimum} or more.",
        section=_THUMBNAILS,
        label="Thumbnail cache limit",
        help="Megabytes. Thumbnails of wallpapers you have judged are never deleted — history needs them "
        "— so this only limits the ones still waiting in the pool. Anything deleted is fetched again the "
        "next time it is shown.",
    )
    active_mix = Field(
        default=DEFAULT_ACTIVE_MIX,
        parse=_mix_name,
        on_form=False,
        admit=_a_stored_mix,
        message="No mix goes by that name — it may have been deleted in another tab.",
    )
    """Which **Mix** the next **Batch** is built from, by name.

    Only the name, so `get` stays one query that never refuses; `active_mix()` resolves it.
    """

    def __init__(self, values: Mapping[str, object]) -> None:
        self.__dict__.update(values)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError(f"Settings are read-only: {name}")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Settings) and self.__dict__ == other.__dict__

    def __hash__(self) -> int:
        return hash(tuple(self.__dict__.items()))

    def __repr__(self) -> str:
        return f"Settings({', '.join(f'{key}={value!r}' for key, value in self.__dict__.items())})"

    @property
    def atleast(self) -> str:
        """The minimum resolution in Wallhaven's `WxH` spelling."""
        return f"{self.min_width}x{self.min_height}"

    @property
    def ratios(self) -> str:
        """The allowed ratios in Wallhaven's comma-separated spelling."""
        return ",".join(self.allowed_ratios)


FIELDS: tuple[Field[Any], ...] = tuple(
    cast(Field[Any], attribute) for attribute in vars(Settings).values() if isinstance(attribute, Field)
)
"""The table: every setting, in declaration order."""

FORM_FIELDS = tuple(field for field in FIELDS if field.on_form)
"""The settings form's fields, in the same order."""


def form_sections() -> tuple[tuple[Section, tuple[Field[Any], ...]], ...]:
    """The settings form as the page lays it out: each section with its fields, both in declaration order."""
    grouped: dict[Section, list[Field[Any]]] = {}
    for field in FORM_FIELDS:
        grouped.setdefault(field.section or _UNHEADED, []).append(field)
    return tuple((section, tuple(fields)) for section, fields in grouped.items())


_BY_KEY = {field.key: field for field in FIELDS}


def seeds() -> dict[str, str]:
    """Every setting's default, as stored: what the seeding migrations write and `get` falls back to."""
    return {field.key: field.text(field.default) for field in FIELDS}


def get(connection: sqlite3.Connection) -> Settings:
    """Everything configurable. Never refuses: a hand-edited bad row falls back to its default, so the
    settings page still opens to fix it.
    """
    stored = {
        str(row["key"]): str(row["value"]) for row in connection.execute("SELECT key, value FROM settings")
    }
    values: dict[str, object] = {}
    for field in FIELDS:
        parsed = field.parse(stored[field.key]) if field.key in stored else field.default
        values[field.key] = field.default if isinstance(parsed, SettingsRefused.Reason) else parsed
    return Settings(values)


def update(write: sqlite3.Connection, **fields: object) -> Settings | SettingsRefused:
    """Validate and store the settings named, on the caller's write handle; `None` leaves a field alone.

    Keywords rather than a whole `Settings`, so a form that renders half the settings cannot reset the other
    half, and there is no read-then-write. Text in, typed out: every field is validated before anything is
    written, so a refusal writes nothing. A keyword the table does not have is a `TypeError`.
    """
    unknown = sorted(fields.keys() - _BY_KEY.keys())
    if unknown:
        raise TypeError(f"no such setting: {', '.join(unknown)}")
    changes: list[tuple[str, str]] = []
    for field in FIELDS:
        posted = fields.get(field.key)
        if posted is None:
            continue
        parsed = field.parse(_as_text(posted))
        refused = parsed if isinstance(parsed, SettingsRefused.Reason) else None
        if refused is None and field.admit is not None:
            refused = field.admit(write, parsed)
        if refused is not None:
            return SettingsRefused(reason=refused, field=field.key)
        # The **Library** path is stored, not created: the writer creates the folder on its first write, so
        # a path typed and undone leaves nothing behind.
        changes.append((field.key, field.text(parsed)))
    write.executemany(_UPSERT_SETTING, changes)
    # Read on the same handle, so what comes back is what this write put there.
    return get(write)


def form_values(current: Settings) -> dict[str, str]:
    """Every settings form field as the text the form shows, by key."""
    return {field.key: field.text(getattr(current, field.key)) for field in FORM_FIELDS}


def _as_text(value: object) -> str:
    """A value as the text a form would post: typed values are accepted too, and a list is comma-joined."""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return ",".join(str(part) for part in cast(Sequence[object], value))
    return str(value)


# -- Mixes ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MixListing:
    """A stored **Mix**, and whether it may be deleted now: neither a default nor the active one."""

    mix: Mix
    deletable: bool


def validated_mix(
    name: str, *, unknown: int | str, banger: int | str, dud: int | str
) -> Mix | SettingsRefused.Reason:
    """A **Mix**, or the reason those numbers are not one. The one place that decides what a **Mix** is.

    Whole percentages, none negative, summing to exactly `MIX_TOTAL`; a name trimmed, non-empty and at most
    `MAX_MIX_NAME_LENGTH`, compared case-sensitively.
    """
    trimmed = name.strip()
    if not trimmed or len(trimmed) > MAX_MIX_NAME_LENGTH:
        return SettingsRefused.Reason.MIX_NAME_INVALID
    percentages = [_whole_number(value) for value in (unknown, banger, dud)]
    if any(share is None or share < 0 for share in percentages):
        return SettingsRefused.Reason.MIX_PERCENTAGES_INVALID
    shares = [share for share in percentages if share is not None]
    if sum(shares) != MIX_TOTAL:
        return SettingsRefused.Reason.MIX_PERCENTAGES_INVALID
    return Mix(name=trimmed, unknown=shares[0], banger=shares[1], dud=shares[2])


def list_mixes(connection: sqlite3.Connection) -> tuple[MixListing, ...]:
    """Every stored **Mix**, by name, or the seeded pair if there are none, each saying if it is deletable.

    A hand-edited row that `validated_mix` refuses is dropped rather than offered.
    """
    rows = connection.execute("SELECT name, unknown, banger, dud FROM mixes ORDER BY name").fetchall()
    stored = [
        mix
        for mix in (
            validated_mix(str(row["name"]), unknown=row["unknown"], banger=row["banger"], dud=row["dud"])
            for row in rows
        )
        if isinstance(mix, Mix)
    ]
    active = get(connection).active_mix
    return tuple(
        MixListing(mix, deletable=mix.name not in UNDELETABLE_MIXES and mix.name != active)
        for mix in stored or DEFAULT_MIXES
    )


def active_mix(connection: sqlite3.Connection) -> Mix:
    """The **Mix** the next **Batch** is built from, or the first there is if the stored name matches none."""
    mixes = [listed.mix for listed in list_mixes(connection)]
    name = get(connection).active_mix
    return next((mix for mix in mixes if mix.name == name), mixes[0])


def save_mix(
    write: sqlite3.Connection, name: str, *, unknown: int | str, banger: int | str, dud: int | str
) -> Mix | SettingsRefused:
    """Store a **Mix** under that name, replacing any already there. Applies to the next **Batch**, not
    this one.
    """
    mix = validated_mix(name, unknown=unknown, banger=banger, dud=dud)
    if isinstance(mix, SettingsRefused.Reason):
        return SettingsRefused(reason=mix)
    write.execute(_UPSERT_MIX, (mix.name, mix.unknown, mix.banger, mix.dud))
    return mix


def delete_mix(write: sqlite3.Connection, name: str) -> SettingsRefused | None:
    """Remove a **Mix**, or say why it stays: unknown, then permanent, then in use, in that order."""
    trimmed = name.strip()
    listed = {listing.mix.name: listing for listing in list_mixes(write)}
    if trimmed not in listed:
        return SettingsRefused(reason=SettingsRefused.Reason.MIX_UNKNOWN)
    if trimmed in UNDELETABLE_MIXES:
        return SettingsRefused(reason=SettingsRefused.Reason.MIX_NOT_DELETABLE)
    if not listed[trimmed].deletable:
        return SettingsRefused(reason=SettingsRefused.Reason.MIX_IN_USE)
    write.execute(_DELETE_MIX, (trimmed,))
    return None


MIX_FORM_FIELDS = ("name", "unknown", "banger", "dud")
"""The keys a **Mix** form posts, the add row and every edit row alike: `save_mix`'s parameters."""


@dataclass(frozen=True, slots=True)
class MixFormRow:
    """One row of the **Mixes** form: the text each input shows, by `MIX_FORM_FIELDS` key, and whether the row
    is the active **Mix** and may be deleted. The add row is never either.
    """

    name: str
    unknown: str
    banger: str
    dud: str
    active: bool = False
    deletable: bool = False


@dataclass(frozen=True, slots=True)
class MixForm:
    """The **Mixes** section of the settings form: a row per stored **Mix**, then the add row."""

    rows: tuple[MixFormRow, ...]
    add: MixFormRow


def mix_form(connection: sqlite3.Connection, posted: Mapping[str, str] | None = None) -> MixForm:
    """What the **Mixes** form shows. `posted` is a refused save put back: on the row of the **Mix** it
    names, matched as `save_mix` would store it, or on the add row if it names none.
    """
    typed = {key: (posted or {}).get(key, "") for key in MIX_FORM_FIELDS}
    typed_name = typed["name"].strip()
    active = get(connection).active_mix
    rows: list[MixFormRow] = []
    for listed in list_mixes(connection):
        mix = listed.mix
        stored = {key: str(getattr(mix, key)) for key in MIX_FORM_FIELDS}
        shown = {**typed, "name": mix.name} if typed_name == mix.name else stored
        rows.append(_mix_form_row(shown, active=mix.name == active, deletable=listed.deletable))
    put_back = posted is not None and typed_name not in {row.name for row in rows}
    return MixForm(rows=tuple(rows), add=_mix_form_row(typed if put_back else dict.fromkeys(typed, "")))


def _mix_form_row(text: Mapping[str, str], *, active: bool = False, deletable: bool = False) -> MixFormRow:
    return MixFormRow(
        name=text["name"],
        unknown=text["unknown"],
        banger=text["banger"],
        dud=text["dud"],
        active=active,
        deletable=deletable,
    )


def _whole_number(value: object) -> int | None:
    """The value as a whole number, or `None`: "1.5" and "1e3" are refused rather than coerced."""
    try:
        return int(str(value).strip())
    except ValueError:
        return None


_MIX_REFUSALS = {
    SettingsRefused.Reason.MIX_PERCENTAGES_INVALID: (
        f"Mix percentages must be whole numbers, 0 or more, that add up to {MIX_TOTAL}."
    ),
    SettingsRefused.Reason.MIX_NAME_INVALID: (
        f"A mix needs a name, of no more than {MAX_MIX_NAME_LENGTH} characters."
    ),
    SettingsRefused.Reason.MIX_UNKNOWN: "No mix goes by that name — it may have been deleted in another tab.",
    SettingsRefused.Reason.MIX_IN_USE: (
        "That is the active mix. Switch to another on the batch page before deleting it."
    ),
    SettingsRefused.Reason.MIX_NOT_DELETABLE: "Explore and refine can be edited but cannot be deleted.",
}
"""A **Mix** refusal in words, as the settings page says it."""

_UPSERT_SETTING = """
INSERT INTO settings (key, value) VALUES (?, ?)
ON CONFLICT (key) DO UPDATE SET value = excluded.value
"""

_UPSERT_MIX = """
INSERT INTO mixes (name, unknown, banger, dud) VALUES (?, ?, ?, ?)
ON CONFLICT (name) DO UPDATE
SET unknown = excluded.unknown, banger = excluded.banger, dud = excluded.dud
"""
"""One statement for creating and editing a **Mix**: the name is the key, so there is no rename."""

_DELETE_MIX = "DELETE FROM mixes WHERE name = ?"
