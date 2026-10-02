"""The Core service: the single seam between the UI and everything else.

Invariant 1 — the UI and every test talk only to this class, and its five dependencies are injected.
Storage lives here too: SQLite is an in-process detail of the Core service, not another seam.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

from wallpapi.allocation import ZONE_ORDER, allocate
from wallpapi.clock import Clock
from wallpapi.files import write_atomically
from wallpapi.library import LibraryWriter
from wallpapi.model import Clearance, DecisionEntry, Mix, Verdict, Wallpaper, Zone
from wallpapi.ratelimit import CALLS_PER_MINUTE, WINDOW_SECONDS, gap_needed, wait_needed
from wallpapi.rng import SeededRandom
from wallpapi.scoring import classify
from wallpapi.similarity import SimilarityProvider
from wallpapi.wallhaven import RateLimited, SearchPage, ThumbnailUnavailable, Wallhaven

SCHEMA_VERSION = 10

SFW_PURITY = "100"
"""Wallhaven's purity mask, most significant bit first: SFW on, sketchy and NSFW off.

Fixed, never a setting. NSFW is what needs an API key, and wallpapi asks for none.
"""

SFW_PURITY_NAME = "sfw"
"""What a search *result* calls the same thing. The mask is a query parameter; this is a response field."""

ALL_CATEGORIES = "111"
"""Wallhaven's category mask: general, anime and people, all on. Fixed, never a setting — the **Filters**
are about the shape and the quality of a **Wallpaper**, not about its subject."""

IDLE_RECHECK_SECONDS = 30.0
"""How long the refill waits before looking again once the **Pool** is at target.

Not zero, because a spin would burn a core doing `SELECT COUNT(*)`, and not minutes, because the **Pool**
drops below target the moment a **Batch** is submitted and the backlog should be topped up before the next
one is asked for.
"""

ERROR_BACKOFF_SECONDS = 60.0
"""How long the refill waits after a failed **API call** that named no delay of its own.

A minute, which is the window the 45-per-minute budget is counted over: if Wallhaven is refusing calls, the
cheapest correct thing to do is to stop spending the budget for one whole window.
"""

THUMBNAIL_IDLE_RECHECK_SECONDS = 30.0
"""How long the thumbnail downloader waits before looking again once no **Pool** member is missing one.

The refill's idle recheck, for the same reasons: not zero, which would spin on a directory listing, and not
minutes, because a submission retires a **Batch**'s worth of the **Pool** and the refill admits newcomers
straight after, each of them work for the downloader (#44).
"""

THUMBNAIL_BACKOFF_SECONDS = 60.0
"""How long the thumbnail downloader leaves the host alone after a 429 or a connection that failed.

The refill's minute, for the same reason: whatever made that fetch fail would make the next one fail too.
A `Retry-After` asking for longer is given longer; one asking for less does not shorten it, because the
minute is the downloader's own manners towards a host with no published limit (invariant 11).
"""

POOL_SOURCE_RANDOM = "random"
POOL_SOURCE_LIKE = "like"
"""How a **Pool** member got there: a random walk, or a like: search on a **Favourite** (#13)."""

LIKE_QUERY_PREFIX = "like:"
"""Wallhaven's own spelling for "wallpapers similar to this one", sent as the `q` of a search.

It lives here rather than in `wallhaven.py` because *which* expression to send is the refill's decision;
the client carries whatever `query` it is handed.
"""

LIKE_SORTING = "relevance"
"""How a like: search is sorted, against `random` for the other strategy.

The point of a like: search is the most similar **Wallpapers** first — the walk is capped at
`LIKE_PAGES_PER_FAVOURITE` precisely because the tail is only weakly similar, and a random sort would mix
that tail through the pages that are worth having. Wallhaven's default of `date_added` would do the same.
"""

LIKE_PAGES_PER_FAVOURITE = 3
"""How far a like: walk goes before moving on to the next **Favourite**.

Three pages is 72 **Wallpapers** at Wallhaven's listing size, which is more lookalikes than most
**Wallpapers** have any real claim to. Paging to the end instead would spend an unbounded share of the
budget on one **Favourite**'s weakly similar tail while the others waited their turn.
"""

MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 64
"""The accepted range for the batch size, inclusive at both ends.

One because a **Batch** of none is a page with nothing to decide on and no way back off it. Sixty-four
because the spec asks for "a big blitz of 16 or 32" and nothing larger.

The upper bound used to be justified by what one page load could fetch — 24 results a page and four capped
**API calls**. That reasoning went with the walk at #6: a **Batch** is now sampled from a **Pool** of
`pool_target_size`, which is 500 by default, so what a **Batch** can be filled from is no longer the
binding constraint. Sixty-four stays because it is as many **Wallpapers** as anyone can judge at once.
"""

DEFAULT_BATCH_SIZE = 8

MIN_POOL_TARGET_SIZE = 1
MAX_POOL_TARGET_SIZE = 20_000
"""The accepted range for the **Pool** target size.

One, because a **Pool** of none is a **Batch** page with nothing on it and no way off, exactly as a
**Batch** size of none would be. Twenty thousand, because invariant 2 sizes the **Similarity provider**'s
matrix off the **Pool**, and every whole-**Pool** scan — **Scoring** at #9, the **Filter** prune here — is
linear in it. Without a ceiling the refill grows the **Pool** for ever and the system gets slower every day
without ever saying why.
"""

DEFAULT_POOL_TARGET_SIZE = 500
"""Enough to draw from, not a backlog to work through (#38, ADR 0016).

Every submission retires what it showed, so the **Pool** is a stream: the refill tops it up as **Batches**
are submitted, a **Batch** of 32 costing about two search pages. It no longer has to be big for there to be
something new to show, and every whole-**Pool** operation — the **Similarity provider**'s matrix, **Scoring**,
the **Filter** prune — is linear in it, so smaller is cheaper on every **Batch** minted. Five hundred leaves
every **Zone** of a **Mix** something to draw from at the largest batch size."""

SUPERSEDED_POOL_TARGET_SIZE = 2000
"""The target ADR 0005 seeded, kept because migration 10 has to recognise it, and used for nothing else.

Large on purpose when it was chosen: nothing left the **Pool**, so the target was the backlog. Once a
submission retires what it showed (ADR 0016), a standing 2000 is only a slower classification."""

MAX_FILTER_PIXELS = 30_000
"""The largest minimum resolution worth accepting, per axis.

Comfortably above anything Wallhaven serves. A minimum above it can only be a typo, and its only effect
would be an empty **Pool** with nothing anywhere to explain why.
"""

DEFAULT_MIN_WIDTH = 2560
DEFAULT_MIN_HEIGHT = 1440
"""1440p, as a minimum rather than an exact size (`atleast`, never `resolutions`).

The spec's monitor is 1440p or 4K. This admits a 1440p wallpaper at its native size and everything larger,
and excludes the 1080p uploads that would have to be upscaled to fill either.
"""

DEFAULT_ALLOWED_RATIOS = ("16x9", "16x10", "21x9")
"""The shapes a desktop monitor actually is: ordinary widescreen, the 16:10 panels, and ultrawide."""

DEFAULT_MIN_FAVOURITES = 10
"""Enough to skip the long tail nobody has ever looked at, low enough not to collapse the **Pool** to a few
hundred famous images. Applied locally — Wallhaven's search has no parameter for it."""

SUPERSEDED_SIMILARITY_RADIUS = 0.5
"""The radius ADR 0007 seeded, kept because migration 9 has to recognise it.

Half the range says "**Wallpapers** more different than alike tell you nothing about each other", which is
the shape of the rule and was the right guess for a provider whose similarities use the whole range. The
embedding provider's do not (ADR 0013), so the guess became wrong when the provider changed. It lives on
here so that the migration can tell an untouched database from one the user has tuned, and it is used for
nothing else.
"""

DEFAULT_SIMILARITY_RADIUS = 0.15
"""How far a decided **Wallpaper**'s influence reaches, as a distance in `[0, 1]`.

Measured, at last, rather than guessed — though against #14's synthetic **Decision log**, so still a
starting point and still a setting. Image-embedding similarities are bunched: mapped into `[0, 1]` they
span about 0.59 to 1.0, so every distance is under 0.41 and the old 0.5 made every decided **Wallpaper** a
neighbour of the whole **Pool**.

**Not the accuracy-maximising value, deliberately.** 0.2 got the sign right on 98% of held-out
**Verdicts** and left 1% of the **Pool** **Unknown**; 0.15 gets 86% and leaves 7%. **Unknown** is not
waste — it is what an **Explore** **Mix** draws three-quarters of a **Batch** from (ADR 0010) — so a
radius that decides everything leaves nothing to explore, and a tool for finding **Wallpapers** you have
not seen would stop being one. See ADR 0013.
"""

DEFAULT_SIMILARITY_DECAY = 4.0
"""How fast that influence fades with distance, as the rate in `exp(-decay * distance)`.

Also a starting point. Four means a **Wallpaper** at the default radius of 0.5 carries `exp(-2)`, about an
eighth of the value it would at distance 0 — a clear falling-off without a cliff. Zero would make every
**Wallpaper** inside the radius count equally, which is the degenerate case the setting deliberately allows.
"""

MAX_SIMILARITY_DECAY = 50.0
"""The largest decay worth accepting. `exp(-50 * d)` is already under `1e-21` at a hundredth of the range,
so anything above it is a **Pool** of **Unknowns** with nothing on the page to explain why."""

DEFAULT_THUMBNAIL_CACHE_MAX_MB = 500
"""The **Thumbnail cache**'s size cap, as a backstop behind verdict-aware eviction (invariant 8).

Five hundred megabytes is thousands of Wallhaven thumbnails — a **Pool** of 500 plus a **History** of
**Explicit Verdicts** does not approach it — so in normal running the cap never fires and eviction is
decided entirely by **Verdict**. It exists for the case eviction cannot reach: a **Pool** target raised a
long way, or a machine that has been judging **Wallpapers** for a year.
"""

BYTES_IN_A_MEGABYTE = 1024 * 1024
"""Mebibytes, spelled the way a file manager spells megabytes. The setting is a rough ceiling on a cache,
not an accounting figure, and matching what Explorer shows matters more than matching SI."""

HISTORY_PAGE_SIZE = 100
"""Rows on one page of **History**.

Fixed rather than a setting. **History** grows an entry per **Wallpaper** per **Batch**, so it is thousands
of **Ignores** within a week of ordinary use; the page needs *a* bound far more than it needs a
configurable one, and a hundred rows is a scroll rather than a wall.
"""

WALLHAVEN_RATIOS = frozenset(
    {"16x9", "16x10", "21x9", "32x9", "48x9", "9x16", "10x16", "9x18", "1x1", "3x2", "4x3", "5x4"}
)
"""The `ratios=` values Wallhaven accepts, as its own search offers them.

Validated against rather than merely checked for being a string, because an unrecognised ratio is not an
error Wallhaven reports — it is a search that quietly returns something other than what was asked for.
"""

RATIO_TOLERANCE = 0.08
"""How far a **Wallpaper**'s own width/height may sit from a named ratio and still count as it.

Wallhaven's `ratios=` filter buckets rather than matching exactly: a 3440x1440 ultrawide is 2.39 and
Wallhaven serves it under `21x9`, which is 2.33. The local check is a backstop against the API returning
something plainly wrong, not a second opinion on its bucketing, so the band is deliberately generous — and
still narrower than half the gap between `16x9` (1.78) and `16x10` (1.60), the closest pair anyone filters
on.
"""

EXPLORE_MIX = Mix(name="explore", unknown=75, banger=20, dud=5)
REFINE_MIX = Mix(name="refine", unknown=25, banger=70, dud=5)
DEFAULT_MIXES = (EXPLORE_MIX, REFINE_MIX)
"""The two **Mixes** the spec names, seeded by migration 7 and editable at #12.

**Explore** casts a wide net and **Refine** narrows it, and the 5% of **Duds** in both is deliberate: a
**Score** is derived, so a **Wallpaper** the **Decision log** currently leans against can stop being a
**Dud** the moment something near it is **Favourited** — and it never will if it is never shown again.

Stored rather than hard-coded, because #12 makes them editable and lets the user add their own. These
values are the seed for an empty database and the fallback for one whose `mixes` table has been emptied by
hand; they are not what the draw reads. `active_mix()` reads the table.
"""

DEFAULT_ACTIVE_MIX = EXPLORE_MIX.name
"""**Explore** to begin with, because a **Decision log** with nothing in it has no **Bangers** to refine
towards. Nothing enforces that: an all-**Unknown** **Pool** would fall back to **Unknown** under any
**Mix**, which is the shortfall rule doing its job rather than a special case for a new database."""

MIX_TOTAL = 100
"""What a **Mix**'s three percentages must sum to. Exactly, never approximately — the slack in an
**Allocation** is the leftover roll, and a **Mix** summing to 99 would make that roll systematic."""

MAX_MIX_NAME_LENGTH = 40
"""How long a **Mix** name may be.

A **Mix** name is a button on the **Batch** page beside every other **Mix**'s, so the limit is about the
switcher staying readable rather than about storage. Generous enough for anything descriptive — "mostly
unknowns, a few duds" is 26 characters — and short enough that one name cannot push the others off the
row.
"""

UNDELETABLE_MIXES = frozenset(mix.name for mix in DEFAULT_MIXES)
"""The **Mixes** that can be edited but never deleted.

**Explore** and **Refine** are the two the spec names, and `CONTEXT.md` gives each a definition of its
own. Editing one is changing what "explore" means to this user, which is what #12 is for; deleting one
would leave the vocabulary with nothing behind it and let the table be emptied to the point where
`list_mixes` is falling back to values nothing stored. Derived from `DEFAULT_MIXES` rather than written
out a second time, so the seed and the protection cannot drift apart.
"""

_BATCH_SIZE = "batch_size"
_LIBRARY_PATH = "library_path"
_POOL_TARGET_SIZE = "pool_target_size"
_MIN_WIDTH = "min_width"
_MIN_HEIGHT = "min_height"
_ALLOWED_RATIOS = "allowed_ratios"
_MIN_FAVOURITES = "min_favourites"
_SIMILARITY_RADIUS = "similarity_radius"
_SIMILARITY_DECAY = "similarity_decay"
_THUMBNAIL_CACHE_MAX_MB = "thumbnail_cache_max_mb"
_ACTIVE_MIX = "active_mix"
"""The `settings` keys. One row per key, with `Settings` as the typed view over them."""


def _default_library_path() -> Path:
    """Where **Favourites** go until the user says otherwise.

    Under Pictures because that is where Windows' own slideshow settings start looking, and in a `wallpapi`
    subfolder because the **Library** is write-only and should never be mixed in with photos.
    """
    return Path.home() / "Pictures" / "wallpapi"


def _defaults() -> dict[str, str]:
    """The seeded value of every setting, as it is stored.

    The single source of truth for what "unconfigured" means: migration 3 seeds from here, and
    `get_settings` falls back to here for a row a hand-edited database has lost. Computed rather than a
    constant because the **Library** default depends on the user's home directory.
    """
    return {
        _BATCH_SIZE: str(DEFAULT_BATCH_SIZE),
        _LIBRARY_PATH: str(_default_library_path()),
        _POOL_TARGET_SIZE: str(DEFAULT_POOL_TARGET_SIZE),
        _MIN_WIDTH: str(DEFAULT_MIN_WIDTH),
        _MIN_HEIGHT: str(DEFAULT_MIN_HEIGHT),
        _ALLOWED_RATIOS: ",".join(DEFAULT_ALLOWED_RATIOS),
        _MIN_FAVOURITES: str(DEFAULT_MIN_FAVOURITES),
        _SIMILARITY_RADIUS: str(DEFAULT_SIMILARITY_RADIUS),
        _SIMILARITY_DECAY: str(DEFAULT_SIMILARITY_DECAY),
        _THUMBNAIL_CACHE_MAX_MB: str(DEFAULT_THUMBNAIL_CACHE_MAX_MB),
        _ACTIVE_MIX: DEFAULT_ACTIVE_MIX,
    }


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything configurable, as one typed value.

    A plain dataclass, not a Pydantic model: this is inside the core, and Pydantic lives at the edges.
    Storage is still one row per key — this is a view over those rows, so a new setting is a new field and
    a new seeded row rather than a migration against a widening table.
    """

    batch_size: int
    library_path: Path
    pool_target_size: int
    min_width: int
    min_height: int
    allowed_ratios: tuple[str, ...]
    min_favourites: int
    similarity_radius: float
    similarity_decay: float
    thumbnail_cache_max_mb: int
    active_mix: str
    """Which **Mix** the *next* **Batch** is built from, by name.

    The name and not a `Mix`, which is the one place this view is deliberately not fully typed. A **Mix**
    lives in a table of its own — there can be more of them at #12 — and resolving the name here would put
    a second query inside `get_settings`, which is read on the refill loop and inside write transactions.
    It would also make `get_settings` able to fail in a new way: a name whose **Mix** has been deleted has
    no typed value to stand for it, and this view never refuses (see the docstring). `active_mix()` on the
    Core service is the resolved one, and it is what the draw uses.
    """

    @property
    def atleast(self) -> str:
        """The minimum resolution in Wallhaven's `WxH` spelling."""
        return f"{self.min_width}x{self.min_height}"

    @property
    def ratios(self) -> str:
        """The allowed ratios in Wallhaven's comma-separated spelling."""
        return ",".join(self.allowed_ratios)


@dataclass(frozen=True)
class SettingsRefused:
    """The update did not happen, and this is why.

    A result rather than an exception, in the style of `SubmissionRefused`, so the settings page has one
    error branch and can render the reason next to the value that caused it.
    """

    class Reason(StrEnum):
        BATCH_SIZE_NOT_A_NUMBER = "batch_size_not_a_number"
        BATCH_SIZE_OUT_OF_RANGE = "batch_size_out_of_range"
        LIBRARY_PATH_EMPTY = "library_path_empty"
        LIBRARY_PATH_NOT_ABSOLUTE = "library_path_not_absolute"
        POOL_TARGET_SIZE_INVALID = "pool_target_size_invalid"
        MIN_WIDTH_INVALID = "min_width_invalid"
        MIN_HEIGHT_INVALID = "min_height_invalid"
        ALLOWED_RATIOS_INVALID = "allowed_ratios_invalid"
        MIN_FAVOURITES_INVALID = "min_favourites_invalid"
        SIMILARITY_RADIUS_INVALID = "similarity_radius_invalid"
        SIMILARITY_DECAY_INVALID = "similarity_decay_invalid"
        """One reason per **Filter** field rather than the two the batch size has.

        The batch size separates "not a number" from "out of range" because the advice differs: one says
        type a whole number, the other says which numbers are allowed. For the **Filters** both mistakes
        have the same one-sentence answer — the sentence that names the accepted shape — so a second reason
        would be a second branch on the page saying the same thing.
        """

        THUMBNAIL_CACHE_MAX_MB_INVALID = "thumbnail_cache_max_mb_invalid"
        """The **Thumbnail cache** cap, on the same one-reason-per-field rule as the **Filters**."""

        ACTIVE_MIX_UNKNOWN = "active_mix_unknown"
        """No stored **Mix** goes by that name. Refused rather than stored and ignored: a switcher that
        accepted anything would leave the **Batch** page naming a **Mix** the draw has never heard of."""

        MIX_NAME_INVALID = "mix_name_invalid"
        MIX_PERCENTAGES_INVALID = "mix_percentages_invalid"
        """The two ways `validated_mix` can refuse a **Mix**, and what `save_mix` answers with (#12).

        Two and not one, so the validator can say *which* of them went wrong: a name that is not a name
        and three numbers that do not add up have nothing to say to each other, and the form's one error
        branch has to put the sentence next to the field it is about.
        """

        MIX_UNKNOWN = "mix_unknown"
        """No stored **Mix** goes by that name, so there is nothing to delete.

        A refusal rather than a silent no-op, for the reason submitting an already-submitted **Batch** is
        one: the second tab needs to be told why nothing happened.
        """

        MIX_IN_USE = "mix_in_use"
        """That **Mix** is the active one. Switch away first.

        The draw does fall back to the first **Mix** there is when the setting names nothing, so deleting
        the active one *could* be allowed — but a delete button that silently changed what the next
        **Batch** is made of is not a delete button, and switching first is one click.
        """

        MIX_NOT_DELETABLE = "mix_not_deletable"
        """**Explore** and **Refine** are editable and permanent (`UNDELETABLE_MIXES`)."""

    reason: Reason


@dataclass(frozen=True, slots=True)
class Batch:
    """The `size` **Wallpapers** shown at once, with the identity the submission will quote back.

    `drafts` carries the **Draft Batch** alongside the **Wallpapers**, keyed by **Wallpaper** ID and holding
    only the ones actually marked — absence is an **Ignore**, so there is nothing to store for the rest. It
    is what the page renders its controls from, so a load after a partial draft shows the marks already set.

    `zones` is the same shape: the **Zone** each **Wallpaper** was drawn from, as it was at mint time.
    Absence means "this **Batch** was minted before **Zones** existed" and the tile renders without a label
    — not a **Zone** of its own, and never recomputed here, because what the tile says is what the draw
    actually used rather than what the **Decision log** says now.
    """

    id: str
    size: int
    created_at: dt.datetime
    wallpapers: tuple[Wallpaper, ...]
    drafts: Mapping[str, Verdict]
    zones: Mapping[str, Zone]


@dataclass(frozen=True)
class BatchUnavailable:
    """No **Batch** could be built. A result rather than an exception, so the UI has one branch.

    Closes #15. Until the **Pool** existed, a transport failure on a **Batch** page load propagated out of
    the Core service and the browser got a 500. Now the page load makes no **API call** at all, so the only
    way to have nothing to show is an empty **Pool** — and the two reasons it can be empty need different
    words on the page. "Nothing has arrived yet" is a matter of waiting; "Wallhaven is unreachable" is not,
    and saying which, and when it last failed, is the difference between a page and a shrug.
    """

    class Reason(StrEnum):
        POOL_EMPTY = "pool_empty"
        """The **Pool** holds nothing the user has not **Banned**, and the refill has not failed."""

        WALLHAVEN_UNREACHABLE = "wallhaven_unreachable"
        """The **Pool** is empty and the last refill attempt failed. `error` says how."""

    reason: Reason
    error: str | None = None
    error_at: dt.datetime | None = None


class RefillStrategy(StrEnum):
    """Which search a refill step makes — and, because they are the same fact, the `source` it tags what
    it admits to the **Pool** with.

    Two strategies feeding two halves of the **Pool**: the random walk (#6) keeps the **Unknown** **Zone**
    stocked with **Wallpapers** nobody has an opinion about, and the like: walk (#13) grows the **Banger**
    **Zone** out of what the user has already **Favourited**. They take strict turns while both have work,
    which is what keeps either from starving the other, and they share one step so that the
    45-calls-per-minute budget covers both without a second limiter.
    """

    RANDOM = POOL_SOURCE_RANDOM
    LIKE = POOL_SOURCE_LIKE


@dataclass(frozen=True, slots=True)
class RefillStatus:
    """What the background **Pool** refill is doing, for the indicator on the **Batch** page.

    Read through the Core service like everything else, so the page has no second seam to the thread and a
    test can assert on it without starting one.
    """

    pool_size: int
    target_size: int
    running: bool
    last_run_at: dt.datetime | None
    last_error: str | None
    last_error_at: dt.datetime | None
    last_strategy: RefillStrategy | None
    """Which of the two searches the last step made, or `None` before any has run.

    On the indicator because "refill fetching" says nothing about *what*, and the two strategies answer
    different questions about an unmoving **Pool**: a stalled like: rotation means there are no
    **Favourites** yet, and that is a thing the user can fix.
    """

    @property
    def at_target(self) -> bool:
        """Whether the refill is idling rather than spending its budget."""
        return self.pool_size >= self.target_size


@dataclass(frozen=True, slots=True)
class ResolvedVerdict:
    """What one **Wallpaper**'s **Decision log** entries come to.

    Both fields because the callers want different halves: #9 sums `value` across a whole **Pool**, and #7
    renders the `verdict` itself. Never called a **Score** — a **Score** is this spread to similar
    **Wallpapers**, which is #9 and not here.
    """

    verdict: Verdict | None
    value: int


@dataclass(frozen=True, slots=True)
class ScoredWallpaper:
    """One **Pool** **Wallpaper**, its **Score** and the **Zone** that **Score** puts it in.

    Derived on every call and never stored (invariant 2). A **Banned** **Wallpaper** is never one of these:
    it is in no **Zone**, so there is no value of `zone` that could describe it.
    """

    wallpaper: Wallpaper
    score: float
    zone: Zone


@dataclass(frozen=True, slots=True)
class LibraryReconciliation:
    """What one pass at making the **Library** agree with the **Decision log** did, by **Wallpaper** ID.

    A result rather than nothing, because the pass runs after the **Decision log** has already committed
    and so cannot raise (see `CoreService.reconcile_library`). `failed` is the only way a caller — or a
    test — can tell "there was nothing to do" from "it was tried and it did not work".
    """

    written: tuple[str, ...]
    removed: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FavouriteDownload:
    """What one "download all **Favourites**" pass did, by **Wallpaper** ID (#14).

    Three outcomes rather than the reconciliation's three, because this pass never deletes: `written` is a
    **Favourite** that had no file, `skipped` one whose recorded file is still on the disk, and `failed`
    one the download or the guard would not let through. The settings page says all three, which is the
    one reason this is a result and not a count — "nothing to do" and "tried and failed" look identical
    from a total.
    """

    written: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One **Wallpaper**'s line in **History**: what it comes to now, and when it was last judged.

    A **Wallpaper** and not an entry. The **Decision log** holds one entry per **Wallpaper** per **Batch**,
    so a listing of entries would show the same image a dozen times over and offer a dozen places to change
    a **Verdict** that has only one current value. **History** is a view over the log, not a print-out of
    it: one row per **Wallpaper** that has ever been judged, carrying its resolved **Verdict**.

    `latest_at` is the `recorded_at` of its latest entry. Display-only, as every timestamp is; the ordering
    it appears to express is really `seq`'s (invariant 4).
    """

    wallpaper: Wallpaper
    resolved: ResolvedVerdict
    latest_at: dt.datetime


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of **History**, with enough about the rest of it to render the paging.

    `total` counts the rows the filter matches, not the rows on this page, because "1 to 100 of 4,312" is
    the only thing that tells the user that paging is happening at all.
    """

    rows: tuple[HistoryRow, ...]
    page: int
    pages: int
    total: int
    verdict: Verdict | None

    @property
    def previous_page(self) -> int | None:
        return self.page - 1 if self.page > 1 else None

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.page < self.pages else None


@dataclass(frozen=True)
class HistoryRefused:
    """A **History** edit did not happen, and this is why.

    A result rather than an exception, in the shape `SubmissionRefused` and `SettingsRefused` already use,
    so the page has one error branch. Every reason here is either a hand-made post or a second tab: the
    controls **History** renders cannot produce any of them.
    """

    class Reason(StrEnum):
        UNKNOWN_WALLPAPER = "unknown_wallpaper"
        """No such **Wallpaper** in this database, so there is nothing to append an entry against."""

    reason: Reason


@dataclass(frozen=True, slots=True)
class ThumbnailEviction:
    """What one pass of **Thumbnail cache** eviction did.

    A result rather than nothing for the same reason `LibraryReconciliation` is one: this runs at the tail
    of `submit_batch` where nothing can be raised, and `over_cap` is the only way to tell "under the cap"
    from "over it and not allowed to do anything about it".
    """

    evicted: tuple[str, ...]
    remaining_bytes: int
    over_cap: bool


@dataclass(frozen=True, slots=True)
class _CachedThumbnail:
    """One file in the **Thumbnail cache**, stat-ed once."""

    wallpaper_id: str
    path: Path
    size: int
    modified_at: float


@dataclass(frozen=True)
class SubmissionRefused:
    """The submission did not happen, and this is why. Never a silent no-op — two tabs is a real case."""

    class Reason(StrEnum):
        UNKNOWN_BATCH = "unknown_batch"
        ALREADY_SUBMITTED = "already_submitted"

    reason: Reason


class _Connections(threading.local):
    """One SQLite connection per thread: `check_same_thread` defaults to `True` and FastAPI's threadpool
    hands out a different thread per request."""

    connection: sqlite3.Connection | None = None


class CoreService:
    def __init__(
        self,
        *,
        db_path: Path,
        wallhaven: Wallhaven,
        library: LibraryWriter,
        similarity: SimilarityProvider,
        random_source: SeededRandom,
        clock: Clock,
    ) -> None:
        self._db_path = db_path
        self._wallhaven = wallhaven
        self._library = library
        self._similarity = similarity
        self._random = random_source
        self._clock = clock
        self._connections = _Connections()

        # The refill's own state, held in the process rather than in storage: it describes a thread that
        # exists only while wallpapi is running, and a walk half-finished at shutdown is worth nothing
        # afterwards (a seed reused across runs returns the same **Wallpapers**). The lock is because the
        # refill thread writes it and request threads read it for the indicator.
        self._refill_lock = threading.Lock()
        self._api_call_times: deque[float] = deque(maxlen=CALLS_PER_MINUTE)
        """The **API call** timestamps the limiter counts — only the ones still inside the window.

        Trimmed by age on every append, because that is what the deque *means*: entries older than
        `WINDOW_SECONDS` can never change what `wait_needed` returns, and a tool meant to run for months at
        45 calls a minute would otherwise accumulate 65,000 floats a day.

        `maxlen` as well as the trim, and not instead of it. The trim is by age and the cap is by count, and
        only the cap holds when time does not move — a frozen clock in a test, or a monotonic clock that
        somehow stalls. Neither alone is both correct and bounded.
        """
        self._walk_seed: str | None = None
        self._walk_page = 1

        # The like: walk, kept entirely apart from the random one. They interleave step by step, so a
        # single seed and page shared between them would have each clobbering the other's place every
        # other call — the random walk would restart for ever and the like: walk would page through
        # somebody else's results.
        self._like_subject: str | None = None
        """The **Favourite** whose lookalikes are being walked, or `None` between walks."""
        self._like_seed: str | None = None
        self._like_page = 1
        self._like_walked: set[str] = set()
        """The **Favourites** already walked this cycle, so every one has a turn before any has a second."""
        self._last_strategy: RefillStrategy | None = None

        self._retry_not_before: float | None = None
        self._refill_last_run: dt.datetime | None = None
        self._refill_last_error: str | None = None
        self._refill_last_error_at: dt.datetime | None = None
        self._refill_thread_running = False

        # The thumbnail downloader's own state (#44). Touched only by its thread, which is the one caller of
        # `thumbnail_wait` and `thumbnail_step`, so it needs no lock.
        self._last_thumbnail_fetch: float | None = None
        self._thumbnails_not_before: float | None = None
        self._thumbnail_pass: deque[tuple[str, Path, str]] = deque()
        """What is left of the current pass round the **Pool**: `(wallpaper id, destination, source URL)`."""

        self._migrate()

    # -- storage ---------------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = self._connections.connection
        if connection is None:
            connection = sqlite3.connect(self._db_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            # Per-connection, and so re-applied every time: neither survives into a new connection.
            connection.execute("PRAGMA foreign_keys = ON")
            self._connections.connection = connection
        return connection

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        """A write transaction, opened with an explicit `BEGIN IMMEDIATE`.

        A transaction that starts as a reader and upgrades to a writer gets `SQLITE_BUSY_SNAPSHOT` with the
        busy handler skipped, so `busy_timeout` would not save it.
        """
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def _migrate(self) -> None:
        """Apply the numbered steps this database has not seen. Idempotent: a second Core service over the
        same file must find nothing to do rather than assume an empty database."""
        connection = self._connect()
        applied = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if applied >= SCHEMA_VERSION:
            return
        # Persists in the file, so it is set once here rather than per connection. Cannot run in a
        # transaction.
        connection.execute("PRAGMA journal_mode = WAL")
        with self._write() as write:
            if applied < 1:
                for statement in _MIGRATION_1:
                    write.execute(statement)
            if applied < 2:
                for statement in _MIGRATION_2:
                    write.execute(statement)
            if applied < 3:
                for statement, parameters in _migration_3():
                    write.execute(statement, parameters)
            if applied < 4:
                for statement in _MIGRATION_4:
                    write.execute(statement)
            if applied < 5:
                for statement, parameters in _migration_5():
                    write.execute(statement, parameters)
            if applied < 6:
                for statement, parameters in _migration_6():
                    write.execute(statement, parameters)
            if applied < 7:
                for statement, mix_parameters in _migration_7():
                    write.execute(statement, mix_parameters)
            if applied < 8:
                for statement, parameters in _migration_8():
                    write.execute(statement, parameters)
            if applied < 9:
                for statement, parameters in _migration_9():
                    write.execute(statement, parameters)
            if applied < 10:
                for statement, parameters in _migration_10():
                    write.execute(statement, parameters)
            write.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- settings --------------------------------------------------------------------------------------

    def get_settings(self) -> Settings:
        """Everything configurable, read in one go.

        Never raises and never refuses. A stored value this Core service would not have accepted cannot
        have been written through `update_settings`, so it is a hand-edited or truncated row; falling back
        to the seeded default keeps the settings page openable, which is where such a row gets fixed.
        """
        stored = {
            str(row["key"]): str(row["value"])
            for row in self._connect().execute("SELECT key, value FROM settings")
        }
        defaults = _defaults()

        def stored_or_seeded(key: str) -> str:
            return stored.get(key, defaults[key])

        def or_default[T](validated: T | SettingsRefused.Reason, fallback: T) -> T:
            return fallback if isinstance(validated, SettingsRefused.Reason) else validated

        return Settings(
            batch_size=or_default(_validated_batch_size(stored_or_seeded(_BATCH_SIZE)), DEFAULT_BATCH_SIZE),
            library_path=or_default(
                _validated_library_path(stored_or_seeded(_LIBRARY_PATH)), _default_library_path()
            ),
            pool_target_size=or_default(
                _validated_pool_target_size(stored_or_seeded(_POOL_TARGET_SIZE)),
                DEFAULT_POOL_TARGET_SIZE,
            ),
            min_width=or_default(_validated_min_width(stored_or_seeded(_MIN_WIDTH)), DEFAULT_MIN_WIDTH),
            min_height=or_default(_validated_min_height(stored_or_seeded(_MIN_HEIGHT)), DEFAULT_MIN_HEIGHT),
            allowed_ratios=or_default(
                _validated_allowed_ratios(stored_or_seeded(_ALLOWED_RATIOS)), DEFAULT_ALLOWED_RATIOS
            ),
            min_favourites=or_default(
                _validated_min_favourites(stored_or_seeded(_MIN_FAVOURITES)), DEFAULT_MIN_FAVOURITES
            ),
            similarity_radius=or_default(
                _validated_similarity_radius(stored_or_seeded(_SIMILARITY_RADIUS)),
                DEFAULT_SIMILARITY_RADIUS,
            ),
            similarity_decay=or_default(
                _validated_similarity_decay(stored_or_seeded(_SIMILARITY_DECAY)), DEFAULT_SIMILARITY_DECAY
            ),
            thumbnail_cache_max_mb=or_default(
                _validated_thumbnail_cache_max_mb(stored_or_seeded(_THUMBNAIL_CACHE_MAX_MB)),
                DEFAULT_THUMBNAIL_CACHE_MAX_MB,
            ),
            # Not checked against the `mixes` table here, on purpose: this view never refuses and never
            # queries a second table. A name whose **Mix** has been deleted survives as far as
            # `active_mix()`, which is where it falls back — and falling back there rather than here means
            # the switcher goes on showing what the user chose instead of silently reading "explore".
            active_mix=stored_or_seeded(_ACTIVE_MIX).strip() or DEFAULT_ACTIVE_MIX,
        )

    def update_settings(
        self,
        *,
        batch_size: int | str | None = None,
        library_path: Path | str | None = None,
        pool_target_size: int | str | None = None,
        min_width: int | str | None = None,
        min_height: int | str | None = None,
        allowed_ratios: Sequence[str] | str | None = None,
        min_favourites: int | str | None = None,
        similarity_radius: float | str | None = None,
        similarity_decay: float | str | None = None,
        thumbnail_cache_max_mb: int | str | None = None,
        active_mix: str | None = None,
    ) -> Settings | SettingsRefused:
        """Validate and persist the settings named, in one write transaction.

        Keyword-only fields rather than a whole `Settings` value, and `None` meaning "leave this one
        alone". Two reasons. A caller that has to hand back every field must read them all first, and the
        read and the write are then two transactions with a lost update between them. And the page will
        grow sections — **Filters** at #6, **Mixes** at #10 — so a form that renders half the settings must
        not reset the other half by omission. Adding a setting is a new keyword and a new field on
        `Settings`; no existing caller changes. (No setting is nullable today; one that ever is needs a
        sentinel here rather than `None`.)

        Values are accepted as strings as well as typed, because the web form posts strings and the
        coercion rule belongs with the validation rule rather than being spelled out again at the edge.

        Everything is validated before anything is written: a good batch size beside a bad path must not
        half-apply and then be reported as a failure.
        """
        changes: list[tuple[str, str]] = []
        if batch_size is not None:
            validated_size = _validated_batch_size(batch_size)
            if isinstance(validated_size, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_size)
            changes.append((_BATCH_SIZE, str(validated_size)))
        if library_path is not None:
            validated_path = _validated_library_path(library_path)
            if isinstance(validated_path, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_path)
            # Stored, not created. The **Library** writer creates the folder on its first write (#5);
            # creating it here would leave a folder behind for every path the user typed and undid.
            changes.append((_LIBRARY_PATH, str(validated_path)))
        if pool_target_size is not None:
            validated_target = _validated_pool_target_size(pool_target_size)
            if isinstance(validated_target, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_target)
            changes.append((_POOL_TARGET_SIZE, str(validated_target)))
        if min_width is not None:
            validated_width = _validated_min_width(min_width)
            if isinstance(validated_width, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_width)
            changes.append((_MIN_WIDTH, str(validated_width)))
        if min_height is not None:
            validated_height = _validated_min_height(min_height)
            if isinstance(validated_height, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_height)
            changes.append((_MIN_HEIGHT, str(validated_height)))
        if allowed_ratios is not None:
            validated_ratios = _validated_allowed_ratios(allowed_ratios)
            if isinstance(validated_ratios, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_ratios)
            # Stored tidy — trimmed, de-duplicated, in the order given — so the query string Wallhaven
            # sees is the one the settings page shows.
            changes.append((_ALLOWED_RATIOS, ",".join(validated_ratios)))
        if min_favourites is not None:
            validated_favourites = _validated_min_favourites(min_favourites)
            if isinstance(validated_favourites, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_favourites)
            changes.append((_MIN_FAVOURITES, str(validated_favourites)))
        if similarity_radius is not None:
            validated_radius = _validated_similarity_radius(similarity_radius)
            if isinstance(validated_radius, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_radius)
            changes.append((_SIMILARITY_RADIUS, str(validated_radius)))
        if similarity_decay is not None:
            validated_decay = _validated_similarity_decay(similarity_decay)
            if isinstance(validated_decay, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_decay)
            changes.append((_SIMILARITY_DECAY, str(validated_decay)))
        if thumbnail_cache_max_mb is not None:
            validated_cap = _validated_thumbnail_cache_max_mb(thumbnail_cache_max_mb)
            if isinstance(validated_cap, SettingsRefused.Reason):
                return SettingsRefused(reason=validated_cap)
            changes.append((_THUMBNAIL_CACHE_MAX_MB, str(validated_cap)))
        if active_mix is not None:
            # The one setting whose validity is a question about another table rather than about the value
            # itself, so it is checked here rather than in a `_validated_*` function of its own. Checked
            # *before* the write transaction opens, which is the same read-then-write race every other
            # settings field is free of. `delete_mix` (#12) is the only thing that can invalidate the
            # answer, it refuses to delete the active **Mix**, and both run on the one writer — so the
            # window is a second tab selecting a **Mix** the first is deleting, which leaves `active_mix`
            # naming nothing and `active_mix()` falling back rather than the page failing.
            if active_mix.strip() not in {mix.name for mix in self.list_mixes()}:
                return SettingsRefused(reason=SettingsRefused.Reason.ACTIVE_MIX_UNKNOWN)
            changes.append((_ACTIVE_MIX, active_mix.strip()))

        with self._write() as write:
            write.executemany(_UPSERT_SETTING, changes)
            # Read inside the transaction, so what comes back is what this write put there.
            updated = self.get_settings()
            self._prune_pool(write, updated)
            return updated

    # -- mixes -----------------------------------------------------------------------------------------

    def list_mixes(self) -> tuple[Mix, ...]:
        """Every stored **Mix**, in the order the switcher shows them.

        Ordered by name, which puts **Explore** before **Refine** and keeps a user-made **Mix** (#12) in a
        fixed place rather than wherever the query planner put it. A row whose percentages have been
        hand-edited into something that is not a **Mix** is dropped rather than returned: the switcher must
        not offer a **Mix** the draw would then have to make sense of, and `validated_mix` is the one place
        that decides what a **Mix** is.

        Falls back to the seeded pair if the table is empty. There is always something to switch between —
        a **Batch** page with no **Mix** at all would be a page with nothing on it and no way to explain
        why.
        """
        rows = self._connect().execute("SELECT name, unknown, banger, dud FROM mixes ORDER BY name")
        stored = [
            mix
            for mix in (
                validated_mix(str(row["name"]), unknown=row["unknown"], banger=row["banger"], dud=row["dud"])
                for row in rows.fetchall()
            )
            if isinstance(mix, Mix)
        ]
        return tuple(stored) if stored else DEFAULT_MIXES

    def active_mix(self) -> Mix:
        """The **Mix** the next **Batch** will be built from.

        The resolved companion to `get_settings().active_mix`, which is only the name. Falls back to the
        first **Mix** there is if the stored name matches none, because the alternative is a **Batch**
        page that cannot mint a **Batch**, and "your chosen **Mix** is gone" is not worth more than a
        **Batch**.

        Hard to reach on purpose: `update_settings` refuses a name no **Mix** answers to and `delete_mix`
        refuses to delete the active one (#12), so the fallback is for a hand-edited database and for the
        one race those two leave — a second tab selecting a **Mix** the first tab is deleting.
        """
        mixes = self.list_mixes()
        name = self.get_settings().active_mix
        return next((mix for mix in mixes if mix.name == name), mixes[0])

    def save_mix(
        self, name: str, *, unknown: int | str, banger: int | str, dud: int | str
    ) -> Mix | SettingsRefused:
        """Store a **Mix** under that name, replacing the one already there. Issue #12.

        Creating and editing are one operation because they are one write: a **Mix** is identified by its
        name, and whether a row was already there is not a question the form can sensibly ask the user.
        So the edit rows on the settings page and the add row post to the same place, and saving
        **Explore**'s percentages under a new name makes a second **Mix** rather than renaming the first.

        `validated_mix` decides what a **Mix** is — the same function `list_mixes` drops a hand-edited row
        with, and deliberately not a second copy of the rule here. A refusal writes nothing at all.

        **The edit applies to the next Batch minted, not the one on screen.** Nothing has to be done to
        make that true: `active_mix()` reads this table when a **Batch** is drawn, exactly as the batch
        size is read then (#4). A save that rerolled the live **Batch** would throw away a **Draft Batch**
        the user was part way through.
        """
        mix = validated_mix(name, unknown=unknown, banger=banger, dud=dud)
        if isinstance(mix, SettingsRefused.Reason):
            return SettingsRefused(reason=mix)
        with self._write() as write:
            write.execute(_UPSERT_MIX, (mix.name, mix.unknown, mix.banger, mix.dud))
        return mix

    def delete_mix(self, name: str) -> SettingsRefused | None:
        """Remove a **Mix**, or say why it stays. Issue #12.

        Three refusals, checked in this order. A name no **Mix** answers to is `MIX_UNKNOWN` — a refusal
        and not a silent no-op, because the second tab needs to be told why nothing happened.
        **Explore** and **Refine** are `MIX_NOT_DELETABLE`, checked before the active one so that the
        answer for a **Mix** that can *never* be deleted is the permanent reason rather than the one the
        user could work around. The active **Mix** is `MIX_IN_USE`: the draw would fall back, but a
        delete that quietly changed what the next **Batch** is made of is not a delete.

        `None` on success, in the style of `set_draft_verdict` and `edit_verdict` — there is no value to
        hand back, and the caller re-reads `list_mixes` to render.
        """
        trimmed = name.strip()
        if trimmed not in {mix.name for mix in self.list_mixes()}:
            return SettingsRefused(reason=SettingsRefused.Reason.MIX_UNKNOWN)
        if trimmed in UNDELETABLE_MIXES:
            return SettingsRefused(reason=SettingsRefused.Reason.MIX_NOT_DELETABLE)
        if trimmed == self.get_settings().active_mix:
            return SettingsRefused(reason=SettingsRefused.Reason.MIX_IN_USE)
        with self._write() as write:
            write.execute(_DELETE_MIX, (trimmed,))
        return None

    # -- batches ---------------------------------------------------------------------------------------

    def get_next_batch(self) -> Batch | BatchUnavailable:
        """The **Batch** waiting to be decided on, sampling the **Pool** only if there isn't one.

        At most one unsubmitted **Batch** exists at a time. Asking again — a page load, a refresh — hands
        back the same one rather than rerolling it, so nothing is stored until there is something to
        decide on.

        **This makes no API call.** The **Wallpapers** are already stored: the background refill put them
        in the **Pool**, filtered, long before anybody asked for a page. That is what closes #15 — there is
        no network call on the page load path left to fail — and it is why the four-call walk this method
        used to do is gone rather than dormant. See ADR 0005.

        The **Pool** is classified once — **Score** and **Zone** for every member that is not **Banned** —
        and the **Batch** is drawn from that by the active **Mix** (`_choose`). Nothing in it has been
        decided: a submission retires everything it showed from the **Pool** (#38, ADR 0016).

        Each chosen **Wallpaper**'s **Zone** is recorded against the **Batch** row in the same transaction
        as the **Batch** itself, so the tile shows the **Zone** it was actually drawn from rather than one
        recomputed from a **Decision log** that has moved on since.

        **That recorded Zone is the Wallpaper's own, never the slot's.** A shortfall means a slot the
        **Mix** asked one **Zone** for was filled from another, and what goes on the tile is where the
        **Wallpaper** came from rather than what the slot wanted: an **Unknown** shown because there were
        no **Bangers** to be had is an **Unknown**, and labelling it **Banger** because of the slot it
        happens to occupy would be the page telling the user something the **Decision log** does not say.
        It also means the **Zones** on a **Batch** need not match the **Mix** — and that is the visible,
        honest sign that a **Zone** ran out.

        The batch size is read when a **Batch** is minted and not afterwards, so changing it applies to the
        next **Batch** rather than rebuilding the one on screen and discarding its **Draft Batch**.

        **A reshown Wallpaper comes up marked with its latest decision (#37, ADR 0015).** Every chosen
        **Wallpaper** whose resolved **Verdict** is an **Explicit Verdict** gets a **Draft Batch** row in the
        same transaction, so leaving its tile alone records that **Verdict** again and unmarking it records
        an **Ignore** that means what it says. Rows rather than a fallback in the template, so absence keeps
        meaning **Ignore** (invariant 6) and `submit_batch` does not need to know. Dormant since #38: a
        submission retires what it showed (ADR 0016), so nothing reshows a decided **Wallpaper** today. It
        stays for when re-evaluation does (#52).
        """
        live = self._live_batch()
        if live is not None:
            return live

        size = self.get_settings().batch_size
        classified = self.classify_pool()
        if not classified:
            return self._nothing_to_show()

        chosen = self._choose(classified, size)
        created_at = self._clock.now()
        batch_id = uuid4().hex

        with self._write() as write:
            # Re-read under the write lock (ADR 0002): two tabs opened at once must not each mint a
            # **Batch**. Cheaper than it was — the classification above is local reads and arithmetic
            # rather than a network call — but the race it closes is the same one.
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
            # Resolved under the write lock, on this thread's one connection, so a **History** edit cannot
            # land between reading a **Verdict** and pre-filling it.
            resolved = self.resolve_verdicts([scored.wallpaper.id for scored in chosen])
            drafts = {
                wallpaper_id: standing.verdict
                for wallpaper_id, standing in resolved.items()
                if standing.verdict is not None and _is_explicit(standing)
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

    def _choose(self, classified: Sequence[ScoredWallpaper], size: int) -> list[ScoredWallpaper]:
        """Which of the classified **Pool** a **Batch** shows: **Allocation** by the active **Mix**.

        Three steps, and each of them is somewhere a reader can check on its own. `allocate` turns the
        **Mix** and the size into slots per **Zone**. `_draw_order` puts each **Zone** into the order it
        will give its **Wallpapers** up in — highest **Score** first for **Bangers**, weighted random for
        **Unknown** and **Dud**. Then the slots are filled from those orders, and whatever a **Zone** could
        not supply is refilled in `ZONE_ORDER`: **Unknown**, then **Banger**, then **Dud**.

        **A shortfall is not an error.** A **Decision log** with nothing in it has no **Bangers** at all,
        so **Explore**'s twenty per cent has nowhere to come from and the whole **Batch** is **Unknown** —
        which is exactly what a first **Batch** should be. If the whole **Pool** cannot fill the size, the
        **Batch** is simply smaller; that was already true of the uniform draw this replaces.

        No **Wallpaper** twice: each **Zone**'s order is walked forwards and never restarted, and a
        **Wallpaper** is in one **Zone**. **Banned** never: `classify_pool` does not return them.

        The result is shuffled before it is handed back, so the grid is not **Unknowns** first and
        **Bangers** last. The **Zone** is on the tile as a label; it should not also be readable from the
        position, and the highest-scoring **Banger** being the same tile every time would be a ranking the
        user never asked to see.
        """
        slots = allocate(self.active_mix(), size, self._random)
        orders = {zone: self._draw_order(zone, classified) for zone in ZONE_ORDER}
        taken = dict.fromkeys(ZONE_ORDER, 0)
        chosen: list[ScoredWallpaper] = []

        def take(zone: Zone, count: int) -> int:
            """Up to `count` more from `zone`, in its draw order. Returns how many there were."""
            available = orders[zone][taken[zone] : taken[zone] + count]
            taken[zone] += len(available)
            chosen.extend(available)
            return len(available)

        shortfall = sum(slots[zone] - take(zone, slots[zone]) for zone in ZONE_ORDER)
        # One pass is enough: by the end of it every **Zone** is either exhausted or the shortfall is met,
        # and a second pass would find nothing a first did not.
        for zone in ZONE_ORDER:
            if shortfall <= 0:
                break
            shortfall -= take(zone, shortfall)
        return self._random.sample(chosen, len(chosen))

    def _draw_order(self, zone: Zone, classified: Sequence[ScoredWallpaper]) -> list[ScoredWallpaper]:
        """The order one **Zone** gives its **Wallpapers** up in, best first.

        An order rather than a sample, so that "take `k`" and "take `k` more because another **Zone** fell
        short" are the same operation and no **Wallpaper** can come out twice.

        **Bangers** are sorted by **Score**, highest first, over an already randomly ordered list — which is
        how ties are broken by the random source rather than by whatever order the **Pool** query returned,
        while staying reproducible under the seed. `sort` is stable, so the random order is what survives
        inside a run of equal **Scores**. **Unknown** and **Dud** keep the random order they were given:
        there is no "best" **Unknown**, and preferring the least negative **Dud** would be ranking by a
        number the user is not being shown.

        Every member is undecided (ADR 0016), so nothing here is weighted by what the user has already
        said about it. The **Revisit weight** that once was (ADR 0012) went with #38.
        """
        members = [scored for scored in classified if scored.zone is zone]
        ordered = self._random.sample(members, len(members))
        if zone is Zone.BANGER:
            ordered.sort(key=lambda scored: scored.score, reverse=True)
        return ordered

    # -- scoring and zones -----------------------------------------------------------------------------

    def classify_pool(self) -> tuple[ScoredWallpaper, ...]:
        """Every **Pool** **Wallpaper** that is not **Banned**, with its **Score** and its **Zone**.

        One call for the whole **Pool**, never one per **Wallpaper**: that is what invariant 2's matrix
        interface is for, and it is why `resolve_verdicts` is plural. Recomputed from the **Decision log**
        every time and cached nowhere, so an edit to the log — a **Batch** submitted, a **Verdict** changed
        at #7 — changes the next classification with nothing to invalidate and no restart.

        The decided set is every **Wallpaper** the **Decision log** mentions whose resolved value is
        non-zero, whether or not it is still in the **Pool**: a **Favourite** that its own submission
        retired (ADR 0016), or that a **Filter** change pruned, still says something about what the user
        likes. **Ignores** are in it — they are mild
        negatives, and they spread like anything else. A **Wallpaper** whose **Ignores** were wiped out by
        a later **Explicit Verdict**, or whose resolution came to zero, is not: it would contribute a
        weighted nothing to every **Score** and only widen the matrix.

        **Banned** **Wallpapers** are excluded from the rows, because a **Banned** **Wallpaper** is in no
        **Zone**. They stay in the *columns*, though — a **Ban** spreads like any other **Verdict**, and
        that is the whole point of banning something.
        """
        pool = self._pool_wallpapers()
        if not pool:
            return ()
        judged = [_wallpaper_from_row(row) for row in self._connect().execute(_SELECT_DECIDED_WALLPAPERS)]
        # One call over the **Pool** and the decided set together — `resolve_verdicts` is plural for
        # exactly this, and two calls would be two scans of the **Decision log** for one answer.
        resolved = self.resolve_verdicts([w.id for w in pool] + [w.id for w in judged])

        candidates = [w for w in pool if resolved[w.id].verdict is not Verdict.BAN]
        decided = [w for w in judged if resolved[w.id].value != 0]
        if not candidates:
            return ()

        settings = self.get_settings()
        classification = classify(
            self._similarity.similarities(candidates, decided),
            [resolved[w.id].value for w in decided],
            radius=settings.similarity_radius,
            decay=settings.similarity_decay,
        )
        return tuple(
            ScoredWallpaper(wallpaper=wallpaper, score=float(score), zone=zone)
            for wallpaper, score, zone in zip(
                candidates, classification.scores, classification.zones, strict=True
            )
        )

    def _nothing_to_show(self) -> BatchUnavailable:
        """Why the **Pool** had nothing, in the words the page needs (#15).

        A recorded refill failure outranks "nothing yet": if Wallhaven could not be reached, that is the
        fact worth telling the user, and it is the reason the **Pool** never filled.
        """
        with self._refill_lock:
            error, error_at = self._refill_last_error, self._refill_last_error_at
        if error is not None:
            return BatchUnavailable(
                reason=BatchUnavailable.Reason.WALLHAVEN_UNREACHABLE, error=error, error_at=error_at
            )
        return BatchUnavailable(reason=BatchUnavailable.Reason.POOL_EMPTY)

    # -- the Similarity provider's own upkeep ------------------------------------------------------------

    def similarity_notice(self) -> str | None:
        """One line for the **Batch** page when the **Similarity provider** is not at full strength.

        Straight through to the provider, and the Core service does not know what the line means — it is
        the page's seam to everything (invariant 1), and #14's embedding provider has to be able to say "I
        am still fetching my model, so these **Scores** are the baseline's" without the Core service
        learning what a model is.
        """
        return self._similarity.notice()

    def similarity_step(self, stop_event: threading.Event) -> float:
        """One step of the **Similarity provider**'s own upkeep; seconds to wait before the next.

        The Core service's part is one thing only: telling the provider where the **Thumbnail cache** is.
        That is the one fact it has that the provider does not, and it is the fact the embedding provider
        needs — the images wallpapi already holds are the ones in there (invariant 8).

        Called from `similarity_loop` on the background thread and nowhere else: for the embedding
        provider this is an 85MiB download and then a model run per **Wallpaper**. A provider with nothing
        to keep up returns `NOTHING_TO_CATCH_UP`, and the whole thing costs one wake-up an hour.
        """
        return self._similarity.catch_up(self.thumbnail_dir, stop_event)

    # -- the Pool and its refill -------------------------------------------------------------------------

    def refill_status(self) -> RefillStatus:
        """What the refill is doing, for the indicator on the **Batch** page."""
        with self._refill_lock:
            return RefillStatus(
                pool_size=self._pool_size(),
                target_size=self.get_settings().pool_target_size,
                running=self._refill_thread_running,
                last_run_at=self._refill_last_run,
                last_error=self._refill_last_error,
                last_error_at=self._refill_last_error_at,
                last_strategy=self._last_strategy,
            )

    def pool_sources(self) -> dict[str, int]:
        """How many **Pool** members each refill strategy put there, keyed by `source`.

        A read of its own rather than a field on `RefillStatus`, for the reason `api_call_window` is one:
        nothing on a page wants the breakdown, and the tests that pin which strategy admitted what need it
        to come through the seam rather than out of storage (invariant 1).
        """
        rows = self._connect().execute("SELECT source, COUNT(*) AS members FROM pool GROUP BY source")
        return {str(row["source"]): int(row["members"]) for row in rows.fetchall()}

    def api_call_window(self) -> list[float]:
        """The **API call** timestamps the limiter is currently counting, oldest first.

        A copy, and a read rather than a field on `RefillStatus`: nothing on a page wants it, and the only
        caller is the test that pins the window staying bounded over a long run. It is here rather than
        being read off the attribute so that even that test enters through the seam (invariant 1).
        """
        with self._refill_lock:
            return list(self._api_call_times)

    def refill_wait(self) -> float:
        """Seconds the refill should wait before calling `refill_step` again.

        The Core service decides how long; the thread does the waiting, cancellably (invariants 11 and 12).
        Splitting it this way is what lets a test assert the 45-per-minute budget and the back-off with a
        fake clock and no thread at all.

        Three things can ask for a wait and the longest wins: the **Pool** being at target (idle, looking
        again in `IDLE_RECHECK_SECONDS`), the rate limiter, and a back-off a failed call asked for.
        """
        now = self._clock.monotonic()
        if self._pool_size() >= self.get_settings().pool_target_size:
            return IDLE_RECHECK_SECONDS
        with self._refill_lock:
            limited = wait_needed(self._api_call_times, now=now)
            backing_off = 0.0 if self._retry_not_before is None else self._retry_not_before - now
        return max(limited, backing_off, 0.0)

    def refill_step(self) -> None:
        """One step of the refill: at most one **API call**, and never an exception.

        A Core service method rather than something inside the thread, so every test here is a plain
        function call with a fake clock and no thread, no sleeping and no race. The thread (`refill.py`) is
        a loop around this and `refill_wait` and nothing else.

        **Two strategies take strict turns.** A random walk stocks the **Unknown** **Zone** and a like:
        search on a **Favourite** grows the **Banger** one (#13). Alternating step by step is what keeps
        either from starving the other, and it keeps the combined refill inside the one limiter for
        nothing: a step is one **API call** whichever strategy takes it. With no **Favourites** there is
        nothing to alternate with and every step is random.

        Never raises, because the thread must not die (#15): a transport failure or a non-200 is recorded
        as the refill's last error, the walk keeps its place so the same page is retried, and the caller
        backs off. The **Pool** being empty because Wallhaven is down is a **Batch unavailable** page, not
        a missing background thread.
        """
        settings = self.get_settings()
        self._mark_refill_run()
        if self._pool_size() >= settings.pool_target_size:
            # At target: the random walk is over. The next one starts from a fresh seed, because reusing a
            # seed across walks returns the same **Wallpapers**.
            self._reset_walk()
            return

        favourites = self._favourites()
        with self._refill_lock:
            strategy = _alternated(self._last_strategy, has_favourites=bool(favourites))
            self._last_strategy = strategy
            if strategy is RefillStrategy.LIKE:
                subject = self._take_up_a_like_walk(favourites)
                seed, page_number = self._like_seed, self._like_page
            else:
                subject = None
                seed, page_number = self._walk_seed, self._walk_page
            self._record_api_call(self._clock.monotonic())
        try:
            page = self._wallhaven.search(
                sorting="random" if subject is None else LIKE_SORTING,
                query=None if subject is None else f"{LIKE_QUERY_PREFIX}{subject}",
                purity=SFW_PURITY,
                categories=ALL_CATEGORIES,
                page=page_number,
                seed=seed,
                atleast=settings.atleast,
                ratios=settings.ratios,
            )
        except RateLimited as limited:
            # Wallhaven's own answer beats the default: it knows when it will start answering again.
            self._record_refill_failure(limited, limited.retry_after or ERROR_BACKOFF_SECONDS)
            return
        except Exception as failure:
            # Deliberately everything. The Wallhaven protocol names one error — `RateLimited` — and
            # anything else the client raises is "the call did not happen". Narrowing this to httpx2's
            # exceptions would put the transport's spelling in the Core service and would let one
            # unexpected type kill the thread (#15).
            self._record_refill_failure(failure, ERROR_BACKOFF_SECONDS)
            return

        self._admit_to_pool(page.wallpapers, settings, source=strategy)
        if strategy is RefillStrategy.LIKE:
            self._advance_like_walk(page)
        else:
            self._advance_walk(page)
        with self._refill_lock:
            self._retry_not_before = None
            self._refill_last_error = None
            self._refill_last_error_at = None

    @contextmanager
    def refill_running(self) -> Generator[None]:
        """Marks the refill as running for as long as the thread's loop is inside this.

        The Core service does not own the thread — the FastAPI lifespan does — so aliveness has to be told
        to it. A context manager rather than a pair of calls, so a loop that dies of something unexpected
        still clears the flag and the indicator says so instead of lying.
        """
        with self._refill_lock:
            self._refill_thread_running = True
        try:
            yield
        finally:
            with self._refill_lock:
                self._refill_thread_running = False

    def _record_api_call(self, at: float) -> None:
        """Note an **API call**, dropping the ones that have aged out of the window.

        Called with `self._refill_lock` already held. Trimming here rather than in `wait_needed` keeps the
        limiter a pure function of what it is given (invariant 11) and keeps the deque's contents honest:
        what it holds is the window, not every call ever made.
        """
        while self._api_call_times and at - self._api_call_times[0] >= WINDOW_SECONDS:
            self._api_call_times.popleft()
        self._api_call_times.append(at)

    def _mark_refill_run(self) -> None:
        with self._refill_lock:
            self._refill_last_run = self._clock.now()

    def _record_refill_failure(self, failure: Exception, backoff: float) -> None:
        """Remember why the last **API call** failed, and how long to leave Wallhaven alone.

        The text is `str(failure)` rather than a traceback: it is going on a page for the person who owns
        the machine, and "what went wrong and when" is the whole of what they can act on.
        """
        with self._refill_lock:
            self._refill_last_error = str(failure) or type(failure).__name__
            self._refill_last_error_at = self._clock.now()
            self._retry_not_before = self._clock.monotonic() + backoff

    def _advance_walk(self, page: SearchPage) -> None:
        """Carry `meta.seed` to the next page of this walk, or start a fresh walk on an empty page.

        Wallhaven's random sorting reshuffles on every call unless the seed is passed back, so a walk that
        dropped it would be as likely to hand back page one again as anything new. `meta.last_page` is
        returned but is not a stop condition worth carrying: on a random SFW search it was 14,055 when the
        fixture was captured. An empty page is the real end of a walk.
        """
        with self._refill_lock:
            if not page.wallpapers:
                self._walk_seed, self._walk_page = None, 1
                return
            self._walk_seed = page.seed or self._walk_seed
            self._walk_page += 1

    def _reset_walk(self) -> None:
        """Forget where the random walk had got to. The like: walk keeps its place deliberately.

        Only the random walk has a reason to start over: its seed is what stops a walk repeating itself,
        and a seed reused across walks returns the same **Wallpapers**. A like: walk has no such trap —
        `like:<id>` answers the same way whenever it is asked — so dropping its place would only mean
        re-fetching page one of a **Favourite** already half walked once the **Pool** falls below target.
        """
        with self._refill_lock:
            self._walk_seed, self._walk_page = None, 1

    def _favourites(self) -> list[str]:
        """Every **Wallpaper** whose *resolved* **Verdict** is **Favourite**, in a fixed order.

        Resolved rather than merely recorded, which is what makes a **Favourite** replaced by a **Like**
        or a **Ban** — or overturned by an **Ignore** — drop out of the like: rotation by itself. There is
        no list of subjects kept anywhere to fall out of step with the **Decision log**.

        Ordered, because the subject of the next walk is drawn by the seeded random source: over an
        unordered query the same seed and the same **Favourites** could pick differently.
        """
        rows = self._connect().execute(_FAVOURITED_AT_LEAST_ONCE, (Verdict.FAVOURITE.value,)).fetchall()
        candidates = [str(row["wallpaper_id"]) for row in rows]
        resolved = self.resolve_verdicts(candidates)
        return [c for c in candidates if resolved[c].verdict is Verdict.FAVOURITE]

    def _take_up_a_like_walk(self, favourites: Sequence[str]) -> str:
        """The **Favourite** whose lookalikes the next like: search asks for. Lock already held.

        Carries on with the walk in progress while its subject is still a **Favourite**, and otherwise
        starts one on a **Favourite** that has not had a turn this cycle. When all of them have, the cycle
        restarts — which is the rule that stops a seeded draw spending every like: step on one
        **Wallpaper** while the rest of the user's taste goes unasked about.
        """
        current = self._like_subject
        if current is not None and current in favourites:
            return current
        # Either there is no walk in progress, or the one there was has stopped being a **Favourite**
        # mid-way. Either way this is a fresh subject, from page one.
        self._like_walked.intersection_update(favourites)
        remaining = [f for f in favourites if f not in self._like_walked]
        if not remaining:
            self._like_walked.clear()
            remaining = list(favourites)
        chosen = self._random.sample(remaining, 1)[0]
        self._like_subject, self._like_seed, self._like_page = chosen, None, 1
        return chosen

    def _advance_like_walk(self, page: SearchPage) -> None:
        """Page on through one **Favourite**'s lookalikes, or hand the next **Favourite** its turn.

        A like: result set is small and its tail is only weakly similar, so the walk ends at whichever
        comes first: an empty page, or `LIKE_PAGES_PER_FAVOURITE`. The subject is then marked as having
        had its turn and the next step picks another.
        """
        with self._refill_lock:
            if page.wallpapers and self._like_page < LIKE_PAGES_PER_FAVOURITE:
                self._like_seed = page.seed or self._like_seed
                self._like_page += 1
                return
            if self._like_subject is not None:
                self._like_walked.add(self._like_subject)
            self._like_subject, self._like_seed, self._like_page = None, None, 1

    def _admit_to_pool(
        self, wallpapers: Sequence[Wallpaper], settings: Settings, *, source: RefillStrategy
    ) -> None:
        """Put the **Wallpapers** that pass every **Filter** into the **Pool**.

        Checked locally against all of them and not only the minimum **Favourites**, which is the one
        Wallhaven cannot do: the API is trusted but not relied upon, and a **Filter** that only exists in a
        query parameter is a **Filter** nothing verifies.

        The `wallpapers` row is upserted and the `pool` row inserted separately, because **Pool**
        membership is its own table: a **Wallpaper** leaves the **Pool** while its **Decision log** entries
        go on referring to it for ever.

        `source` is the strategy that found it. A **Wallpaper** the other strategy already put in the
        **Pool** keeps the `source` and the `fetched_at` it arrived with — meeting it again is not a second
        arrival — which is what `_ADMIT_TO_POOL`'s `DO NOTHING` is for.

        **A Wallpaper the Decision log mentions is refused (#38, ADR 0016).** Decided once: a walk that
        rediscovers one does not put it back, and a refused **Wallpaper** does not count towards the
        **Pool**, so the refill goes on until it has found ones nobody has seen. Its `wallpapers` row is
        still refreshed, which is harmless — it is the same image, and **History** renders from it.
        """
        passing = [w for w in _distinct(wallpapers) if _passes_filters(w, settings)]
        if not passing:
            return
        fetched_at = self._clock.now().isoformat()
        with self._write() as write:
            write.executemany(_UPSERT_WALLPAPER, [_wallpaper_row(w) for w in passing])
            write.executemany(
                _ADMIT_TO_POOL,
                [{"id": w.id, "fetched_at": fetched_at, "source": source.value} for w in passing],
            )

    def _prune_pool(self, write: sqlite3.Connection, settings: Settings) -> None:
        """Drop every **Pool** member that no longer passes the **Filters**.

        The rule the **Pool** keeps is "no **Wallpaper** that fails the current **Filters**", so this runs
        after every settings write rather than only after one that named a **Filter** — one rule, rather
        than a rule plus a list of which fields count. With nothing changed there is nothing to prune.

        Decided **Wallpapers** go too. **Pool** membership governs only what may be *shown*, so a **Liked**
        1080p **Wallpaper** must stop appearing once the minimum is raised to 1440p exactly as an undecided
        one does — and an **Ignored** one certainly must. Nothing is lost by it: only the membership row
        goes. The `wallpapers` row and every **Decision log** entry stay, so **History** at #7 still renders
        each of them, and an edit there works on the log rather than on **Pool** membership.

        The live unsubmitted **Batch** is untouched for free — a **Batch** holds `batch_wallpapers` rows,
        not **Pool** membership — so nobody loses the **Draft Batch** they are part way through.
        """
        rows = write.execute(_SELECT_POOL_WALLPAPERS).fetchall()
        failing = [
            (str(row["id"]),) for row in rows if not _passes_filters(_wallpaper_from_row(row), settings)
        ]
        if failing:
            write.executemany("DELETE FROM pool WHERE wallpaper_id = ?", failing)

    def _pool_size(self) -> int:
        return int(self._connect().execute("SELECT COUNT(*) FROM pool").fetchone()[0])

    def _pool_wallpapers(self) -> list[Wallpaper]:
        """Every **Wallpaper** in the **Pool**, ordered so a seeded random source draws the same sample."""
        rows = self._connect().execute(_SELECT_POOL_WALLPAPERS).fetchall()
        return [_wallpaper_from_row(row) for row in rows]

    def _live_batch(self) -> Batch | None:
        """The unsubmitted **Batch**, if there is one."""
        return _load_live_batch(self._connect())

    def set_draft_verdict(
        self, batch_id: str, wallpaper_id: str, verdict: Verdict | None
    ) -> SubmissionRefused | None:
        """Mark one tile of the **Draft Batch**, or clear it with `None`.

        Sets rather than toggles (invariant 6): a replayed or duplicated click writes the same row twice
        rather than flipping the state the wrong way round. Clearing deletes the row, because absence
        already means **Ignore** and a stored "none" would be a second way to say the same thing.

        Writes nothing to the **Decision log** — a **Draft Batch** is not the **Decision log**. Drafting
        against an unknown or already submitted **Batch** is refused for the same two reasons submitting
        already uses, so the UI keeps one error branch rather than growing a second.
        """
        refusal: SubmissionRefused | None = None
        with self._write() as write:
            batch = write.execute("SELECT submitted_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                refusal = SubmissionRefused(reason=SubmissionRefused.Reason.UNKNOWN_BATCH)
            elif batch["submitted_at"] is not None:
                refusal = SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)
            elif verdict is None:
                write.execute(
                    "DELETE FROM draft_batch WHERE batch_id = ? AND wallpaper_id = ?",
                    (batch_id, wallpaper_id),
                )
            else:
                write.execute(
                    "INSERT INTO draft_batch (batch_id, wallpaper_id, verdict) VALUES (?, ?, ?)"
                    " ON CONFLICT (batch_id, wallpaper_id) DO UPDATE SET verdict = excluded.verdict",
                    (batch_id, wallpaper_id, verdict.value),
                )
        return refusal

    def set_all_draft_verdicts(self, batch_id: str, verdict: Verdict | None) -> SubmissionRefused | None:
        """Rewrite the whole **Draft Batch** in one transaction: select-all, or select-none with `None`.

        One operation rather than one per tile (invariant 6). At a **Batch** size of 32 the per-tile
        version is 32 posts and 32 transactions for one click, and it can interleave with an in-flight
        single-tile post and leave the **Draft Batch** half-changed. Here the old rows go and the new ones
        arrive inside one `BEGIN IMMEDIATE`, so nothing can read the **Batch** part-marked.

        The rows are built from what the **Batch** *shows*, not from what is already drafted: "all" means
        every tile on screen, including the ones with no mark yet. `None` deletes every row rather than
        writing explicit nothings, because absence already means **Ignore**.

        Refused for an unknown or already submitted **Batch**, with the reasons a single mark already
        uses. **Ignore** is refused at the web edge exactly as it is for a single mark, so the **Verdict**
        never reaches this method.
        """
        refusal: SubmissionRefused | None = None
        with self._write() as write:
            batch = write.execute("SELECT submitted_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                refusal = SubmissionRefused(reason=SubmissionRefused.Reason.UNKNOWN_BATCH)
            elif batch["submitted_at"] is not None:
                refusal = SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)
            else:
                # Scoped to this **Batch**. An unscoped delete reads the same on a database holding one
                # **Draft Batch** and is a silent data loss on any that holds two.
                write.execute("DELETE FROM draft_batch WHERE batch_id = ?", (batch_id,))
                if verdict is not None:
                    write.execute(_MARK_WHOLE_BATCH, (verdict.value, batch_id))
        return refusal

    def submit_batch(self, batch_id: str) -> Batch | BatchUnavailable | SubmissionRefused:
        """Append the **Batch**'s **Verdicts** to the **Decision log**, then hand back the next **Batch**.

        The **Draft Batch** supplies the **Explicit Verdicts**; every **Wallpaper** shown and not marked is
        an **Ignore**, derived here rather than stored, because absence already means **Ignore**. Returning
        the next **Batch** is what lets the user keep going without a reload.

        One transaction, and the **Batch** is claimed inside it: the check and the append cannot be split
        by a second browser tab, because `BEGIN IMMEDIATE` takes the write lock before the read.

        **Everything shown leaves the Pool in that same transaction (#38, ADR 0016)**, **Ignores** included:
        a decision is made once, and **History** is the only way to revisit it. That is what makes room for
        the refill — before it, a **Pool** at target stayed there for good and the refill never ran again.
        """
        recorded_at = self._clock.now().isoformat()
        with self._write() as write:
            batch = write.execute("SELECT submitted_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                return SubmissionRefused(reason=SubmissionRefused.Reason.UNKNOWN_BATCH)
            if batch["submitted_at"] is not None:
                return SubmissionRefused(reason=SubmissionRefused.Reason.ALREADY_SUBMITTED)

            shown = write.execute(
                "SELECT wallpaper_id FROM batch_wallpapers WHERE batch_id = ? ORDER BY position",
                (batch_id,),
            ).fetchall()
            drafted = _load_drafts(write, batch_id)
            write.executemany(
                "INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, ?, ?, ?)",
                [
                    (
                        row["wallpaper_id"],
                        batch_id,
                        drafted.get(str(row["wallpaper_id"]), Verdict.IGNORE).value,
                        recorded_at,
                    )
                    for row in shown
                ],
            )
            # Decided once (#38, ADR 0016): everything shown is now in the **Decision log**, so it leaves
            # the **Pool** in the same transaction that put it there. That is what makes room for the
            # refill, and it stays a decided column of every **Score** (ADR 0007).
            write.executemany(
                "DELETE FROM pool WHERE wallpaper_id = ?", [(row["wallpaper_id"],) for row in shown]
            )
            # Discarded with the submission that consumed it. Left behind, these rows would accumulate
            # against **Batches** that can never be drafted against again (invariant 6).
            write.execute("DELETE FROM draft_batch WHERE batch_id = ?", (batch_id,))
            write.execute("UPDATE batches SET submitted_at = ? WHERE id = ?", (recorded_at, batch_id))

        # Outside the transaction above, deliberately. Downloading a **Favourite** is a network call and no
        # network call belongs inside a write transaction; and a **Favourite** is a fact about taste, not
        # about a download succeeding, so nothing the **Library** does may roll the **Decision log** back.
        # The reconciliation is idempotent, so a failure here is picked up by the next submission.
        self.reconcile_library()
        # And then the **Thumbnail cache**, which is where a submission changes what may be evicted: every
        # **Wallpaper** just shown now has a **Verdict**, and the ones that resolve to an **Ignore** and
        # have left the **Pool** will never be asked for again. Before `get_next_batch`, so the **Batch**
        # about to be minted is not being drawn while its **Wallpapers** are counted as evictable — they
        # are **Pool** members either way, but the ordering says so rather than relying on it.
        self.evict_thumbnails()
        return self.get_next_batch()

    # -- library ---------------------------------------------------------------------------------------

    def reconcile_library(self) -> LibraryReconciliation:
        """Make the **Library** folder agree with the **Decision log**, and report what that took.

        Derived, not a side effect of a click. Every **Wallpaper** whose resolved **Verdict** is
        **Favourite** and which has no recorded **Library** file gets one; every recorded file whose
        **Wallpaper** is no longer a **Favourite** — replaced by a **Like**, a **Ban** or an
        **Ignore** — is deleted. Nothing else is touched.

        That makes this idempotent, which is the whole point. Calling it twice writes once. A download that
        fails is not bookkept as failed and retried later; it is simply still a **Favourite** with no file,
        so the next call picks it up. And because the condition is read from the **Decision log** rather
        than from a queue, there is no state that can drift out of step with it.

        Failures are collected rather than raised. This runs at the tail of `submit_batch`, after the
        **Decision log** transaction has committed: a raise here would turn a recorded **Batch** into an
        error page, and the **Verdicts** would be right while the user was told they were not. They are
        reported instead, so a caller that wants to say something can, and so a test can see the
        difference between "nothing to do" and "tried and failed".

        The **Library** is never read back, so a file deleted in Explorer is not noticed and not replaced.
        Nothing here stats a path; `download_favourites` is the one operation that does (#14).
        """
        library_path = self.get_settings().library_path
        rows = self._connect().execute(_LIBRARY_CANDIDATES, (Verdict.FAVOURITE.value,)).fetchall()
        resolved = self.resolve_verdicts([str(row["wallpaper_id"]) for row in rows])

        written: list[str] = []
        removed: list[str] = []
        failed: list[str] = []
        for row in rows:
            wallpaper_id = str(row["wallpaper_id"])
            recorded = None if row["path"] is None else Path(str(row["path"]))
            wanted = resolved[wallpaper_id].verdict is Verdict.FAVOURITE
            try:
                if wanted and recorded is None:
                    if self._add_to_library(wallpaper_id, str(row["full_url"]), library_path):
                        written.append(wallpaper_id)
                    else:
                        failed.append(wallpaper_id)
                elif not wanted and recorded is not None:
                    deleted = self._drop_from_library(wallpaper_id, recorded, library_path)
                    # A row that points outside the **Library** folder is dropped with the file left
                    # exactly where it is (#14), so there is nothing to report as removed.
                    if deleted:
                        removed.append(wallpaper_id)
            except Exception:
                # The writer is across a seam that declares no error type, so there is nothing narrower to
                # catch: a download can fail with anything httpx2 raises and a write with anything the
                # filesystem does. Leaving the row as it was is what makes the next call retry.
                failed.append(wallpaper_id)
        return LibraryReconciliation(written=tuple(written), removed=tuple(removed), failed=tuple(failed))

    def download_favourites(self) -> FavouriteDownload:
        """Write a **Library** file for every **Favourite** that has not got one. One-way sync (#14).

        **The one place wallpapi reads the folder**, and it reads it for one thing only: whether a path it
        recorded itself is still there. It never lists the folder, never looks for a file it did not write
        and never deletes. That keeps ADR 0006's guarantee intact — a **Library** file deleted in Explorer
        still changes nothing about the **Decision log** — while making the **Library** recoverable from
        the log in a single action, which is what a new machine and a copied database need.

        A **Favourite** counts as missing its file when there is no `library_files` row at all, when the
        recorded path is not on the disk, or when it no longer resolves inside the **Library** folder. The
        second is the case `reconcile_library` deliberately cannot see: the row says the file is there and
        only a stat says otherwise. The third is the **Library path** having changed — a file wallpapi has
        already said it will not so much as delete is not one the **Library** can be said to hold, so it
        counts as absent and the **Favourite** is written into the folder the user is pointing at now.
        Either way the file lands where the **Library path** points *today* and the row is replaced with
        where it landed, which is how a **Library** follows its setting to a new folder in one action.

        Idempotent, like the reconciliation: running it twice writes once and skips the second time. A
        failure is reported rather than raised, and is simply picked up by the next run — there is no
        retry counter, because a **Favourite** with no file already says everything a retry needs to know.
        """
        library_path = self.get_settings().library_path
        rows = self._connect().execute(_LIBRARY_CANDIDATES, (Verdict.FAVOURITE.value,)).fetchall()
        resolved = self.resolve_verdicts([str(row["wallpaper_id"]) for row in rows])

        written: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for row in rows:
            wallpaper_id = str(row["wallpaper_id"])
            if resolved[wallpaper_id].verdict is not Verdict.FAVOURITE:
                continue
            recorded = None if row["path"] is None else Path(str(row["path"]))
            # The guard first, so the only paths ever stat-ed are ones inside the **Library** folder, and
            # then `Path.exists` — which answers False for a path the filesystem will not even parse and
            # for a broken symlink, both of which are "there is no file here" as far as a **Favourite**
            # is concerned.
            held = None if recorded is None else confined_to_library(recorded, library_path)
            if held is not None and held.exists():
                skipped.append(wallpaper_id)
                continue
            try:
                if self._add_to_library(wallpaper_id, str(row["full_url"]), library_path):
                    written.append(wallpaper_id)
                else:
                    failed.append(wallpaper_id)
            except Exception:
                # As in `reconcile_library`: the writer declares no error type, and a **Favourite** with
                # no file is its own record of what still has to happen.
                failed.append(wallpaper_id)
        return FavouriteDownload(written=tuple(written), skipped=tuple(skipped), failed=tuple(failed))

    def _add_to_library(self, wallpaper_id: str, source_url: str, library_path: Path) -> bool:
        """Download one **Favourite** and record where it landed, or refuse to before anything is fetched.

        The destination is computed from the **Library** setting as it is *now*, and the path recorded is
        the one the writer returned rather than the one it was handed — the writer is what knows where the
        bytes actually went.

        `False` — and no network call at all — when the name or the destination does not pass the guard
        (invariant 9). The check comes before the download deliberately: a refusal that had already
        fetched several megabytes would be a refusal in name only.
        """
        name = library_file_name(wallpaper_id, source_url)
        if name is None:
            return False
        destination = confined_to_library(library_path / name, library_path)
        if destination is None:
            return False
        written = self._library.write(wallpaper_id, source_url, destination)
        with self._write() as write:
            write.execute(_RECORD_LIBRARY_FILE, (wallpaper_id, str(written), self._clock.now().isoformat()))
        return True

    def _drop_from_library(self, wallpaper_id: str, recorded: Path, library_path: Path) -> bool:
        """Forget a recorded **Library** file, and delete it if it is still one wallpapi may delete.

        `recorded` comes from the row, never recomputed from the current **Library** setting (invariant 9):
        the path is where the file was actually written. What #14 adds is that being recorded is no longer
        enough on its own — the path must *also* still resolve inside the **Library** folder the setting
        names today. A row pointing anywhere else, whether because the setting changed or because
        something odd was recorded, is dropped with the file left exactly where it is.

        The row goes either way, and that is the deliberate part. Keeping it would mean refusing the same
        path again on every submission for ever, and the row's only meaning is "wallpapi holds a file for
        this **Wallpaper**" — which, once wallpapi will not touch it, is no longer true.

        Returns whether the file was handed to the writer to delete. A failed deletion raises out of here
        with the row intact, so it is retried rather than forgotten about.
        """
        confined = confined_to_library(recorded, library_path)
        if confined is not None:
            self._library.remove(confined)
        with self._write() as write:
            write.execute("DELETE FROM library_files WHERE wallpaper_id = ?", (wallpaper_id,))
        return confined is not None

    # -- thumbnails ------------------------------------------------------------------------------------

    @property
    def thumbnail_dir(self) -> Path:
        """The **Thumbnail cache** directory. Deliberately separate from the **Library**, which is
        favourites-only and write-only."""
        return self._db_path.parent / "thumbnails"

    def get_thumbnail(self, wallpaper_id: str) -> Path | None:
        """The cached thumbnail for a **Wallpaper**, fetching it the first time and never again.

        `None` for a **Wallpaper** this database has never seen. An evicted thumbnail is fetched again
        here, which is what makes eviction safe to be aggressive about: the cost of getting it wrong is one
        request to `th.wallhaven.cc`, not a broken page.
        """
        row = (
            self._connect()
            .execute("SELECT thumbnail_url FROM wallpapers WHERE id = ?", (wallpaper_id,))
            .fetchone()
        )
        if row is None:
            return None

        source_url = str(row["thumbnail_url"])
        destination = self.thumbnail_dir / f"{wallpaper_id}{_url_suffix(source_url)}"
        if destination.exists():
            return destination

        data = self._wallhaven.fetch_thumbnail(source_url)
        write_atomically(destination, data)
        return destination

    def thumbnail_wait(self) -> float:
        """Seconds the thumbnail downloader should wait before calling `thumbnail_step` again."""
        now = self._clock.monotonic()
        paced = gap_needed(self._last_thumbnail_fetch, now=now)
        held = 0.0 if self._thumbnails_not_before is None else self._thumbnails_not_before - now
        return max(paced, held, 0.0)

    def thumbnail_step(self) -> None:
        """One step of the thumbnail downloader: at most one fetch from the thumbnail host, never raising."""
        now = self._clock.monotonic()
        if not self._thumbnail_pass:
            if self._thumbnail_cache_full():
                self._thumbnails_not_before = now + THUMBNAIL_IDLE_RECHECK_SECONDS
                return
            self._thumbnail_pass.extend(self._pool_missing_thumbnails())
            if not self._thumbnail_pass:
                self._thumbnails_not_before = now + THUMBNAIL_IDLE_RECHECK_SECONDS
                return

        wallpaper_id, destination, source_url = self._thumbnail_pass.popleft()
        if not self._thumbnail_pass:
            # The end of a pass. Whatever this one failed to fetch is asked for once a pass, not as fast as
            # the gap allows: a file the host keeps refusing must not be requested four times a second.
            self._thumbnails_not_before = now + THUMBNAIL_IDLE_RECHECK_SECONDS
        if destination.exists() or not self._in_pool(wallpaper_id):
            return

        self._last_thumbnail_fetch = now
        try:
            data = self._wallhaven.fetch_thumbnail(source_url)
        except ThumbnailUnavailable:
            return
        except RateLimited as limited:
            self._thumbnails_not_before = now + max(limited.retry_after or 0.0, THUMBNAIL_BACKOFF_SECONDS)
            return
        except Exception:
            self._thumbnails_not_before = now + THUMBNAIL_BACKOFF_SECONDS
            return
        write_atomically(destination, data)

    def _pool_missing_thumbnails(self) -> list[tuple[str, Path, str]]:
        """Every **Pool** member with no file in the **Thumbnail cache**, in `wallpaper_id` order."""
        missing: list[tuple[str, Path, str]] = []
        for row in self._connect().execute(_SELECT_POOL_THUMBNAILS).fetchall():
            wallpaper_id, source_url = str(row["id"]), str(row["thumbnail_url"])
            destination = self.thumbnail_dir / f"{wallpaper_id}{_url_suffix(source_url)}"
            if not destination.exists():
                missing.append((wallpaper_id, destination, source_url))
        return missing

    def _thumbnail_cache_full(self) -> bool:
        """Whether the **Thumbnail cache** is at or over `thumbnail_cache_max_mb`. Asked once a pass."""
        directory = self.thumbnail_dir
        held = sum(cached.size for cached in _cached_thumbnails(directory)) if directory.is_dir() else 0
        return held >= self.get_settings().thumbnail_cache_max_mb * BYTES_IN_A_MEGABYTE

    def _in_pool(self, wallpaper_id: str) -> bool:
        return (
            self._connect().execute("SELECT 1 FROM pool WHERE wallpaper_id = ?", (wallpaper_id,)).fetchone()
            is not None
        )

    def evict_thumbnails(self) -> ThumbnailEviction:
        """Clear out the **Thumbnail cache**, by **Verdict** first and by size only as a backstop.

        Invariant 8. Eviction is *not* "when it leaves the **Pool**": **History** renders a thumbnail for
        every past **Verdict**, and a **Banned Wallpaper** leaves every **Zone** immediately and
        permanently while still needing a picture on the page where the **Ban** can be undone.

        Two passes, and the order matters.

        The first deletes every cached thumbnail whose **Wallpaper** has no **Explicit Verdict** standing,
        is not in the **Pool** and is not in the live **Batch**. That is exactly the set nothing will ever
        ask for again: it is not going to be shown, and it is not in **History** as anything but an
        **Ignore**. It also sweeps the leftovers of **Batches** abandoned before ADR 0002 — their
        **Wallpapers** have neither a **Verdict** nor a place in the **Pool**, so their thumbnails go, and
        so do files whose `wallpapers` row was never written at all.

        The second is the size cap, and it exists because the first pass cannot see the common case of a
        cache that is simply large: a **Pool** of thousands of undecided **Wallpapers**, every one of them
        still waiting to be shown. Over `thumbnail_cache_max_mb`, the oldest-modified of *those* go until
        the cache is under it — each costs one re-fetch when it is next asked for.

        **A thumbnail whose Wallpaper has an Explicit Verdict is never evicted by either pass.** That is
        the rule invariant 8 states and the cap does not get to break it, which means a cache that is over
        the cap on **Favourites** alone stays over it. `over_cap` says so rather than hiding it.

        Called at the tail of `submit_batch`, and cheap when there is nothing to do: an empty or absent
        directory returns immediately, and in ordinary running every file belongs to a **Pool** member or a
        decided **Wallpaper**, so the first pass deletes nothing and the second never runs.
        """
        directory = self.thumbnail_dir
        if not directory.is_dir():
            return _NOTHING_EVICTED
        cached = _cached_thumbnails(directory)
        if not cached:
            return _NOTHING_EVICTED

        decided = self._explicitly_decided()
        awaiting = self._awaiting_a_verdict()
        evicted: list[str] = []
        capped: list[_CachedThumbnail] = []
        remaining = 0
        for thumbnail in cached:
            if thumbnail.wallpaper_id in decided:
                remaining += thumbnail.size
            elif thumbnail.wallpaper_id in awaiting:
                # Still to be shown, so kept unless the cap says otherwise.
                capped.append(thumbnail)
                remaining += thumbnail.size
            else:
                thumbnail.path.unlink(missing_ok=True)
                evicted.append(thumbnail.wallpaper_id)

        cap = self.get_settings().thumbnail_cache_max_mb * BYTES_IN_A_MEGABYTE
        # Oldest modified first, with the name as the tie-break: two files written in the same frozen
        # second must not be evicted in whatever order the directory happened to list them.
        for thumbnail in sorted(capped, key=lambda cached: (cached.modified_at, cached.path.name)):
            if remaining <= cap:
                break
            thumbnail.path.unlink(missing_ok=True)
            evicted.append(thumbnail.wallpaper_id)
            remaining -= thumbnail.size

        return ThumbnailEviction(evicted=tuple(evicted), remaining_bytes=remaining, over_cap=remaining > cap)

    def _explicitly_decided(self) -> set[str]:
        """The **Wallpapers** whose resolved **Verdict** is an **Explicit Verdict**.

        Bounded by what the user has actually judged rather than by the **Pool**, and read through the same
        resolution CTE as everything else — so a **Favourite** overturned by an **Ignore** is not in it, and
        an un-**Banned** **Wallpaper** stops being in it the moment that **Ignore** lands.
        """
        return {str(row["wallpaper_id"]) for row in self._connect().execute(_EXPLICITLY_DECIDED)}

    def _awaiting_a_verdict(self) -> set[str]:
        """The **Wallpapers** that are still going to be put in front of the user: the **Pool** and the
        live **Batch**. The live **Batch** is named separately because a **Wallpaper** can be on screen
        after being pruned out of the **Pool** by a **Filter** change."""
        return {str(row["wallpaper_id"]) for row in self._connect().execute(_AWAITING_A_VERDICT)}

    # -- verdict resolution ----------------------------------------------------------------------------

    def resolve_verdicts(self, wallpaper_ids: Sequence[str]) -> dict[str, ResolvedVerdict]:
        """**Verdict resolution** for many **Wallpapers** at once, derived and never stored.

        Plural because #9 scores a whole **Pool** in one go, and a per-**Wallpaper** call would force a
        Python loop over 10k rows. Derived on every call because **Scores** are never stored (invariant 2).

        A **Wallpaper** with no entries, one whose latest entry is a legacy **Clearance**, and one this
        database has never seen all resolve to absent and zero.
        """
        requested = list(dict.fromkeys(wallpaper_ids))
        resolved = dict.fromkeys(requested, _ABSENT)
        if not requested:
            return resolved
        # One query rather than one per **Wallpaper**. SQLite's parameter limit is 32,766 here, well above
        # the 10k **Pool** #9 will hand in.
        placeholders = ",".join("?" * len(requested))
        query = _resolution_query(
            "SELECT wallpaper_id, resolved FROM resolution",
            restriction=f"WHERE wallpaper_id IN ({placeholders})",
        )
        for row in self._connect().execute(query, requested).fetchall():
            resolved[str(row["wallpaper_id"])] = _resolved_from(row["resolved"])
        return resolved

    # -- history ---------------------------------------------------------------------------------------

    def edit_verdict(self, wallpaper_id: str, verdict: Verdict) -> HistoryRefused | None:
        """Change a **Wallpaper**'s **Verdict** from **History** by appending a new entry.

        Appended, never a rewrite. The **Decision log** is the single source of truth and it is append-only
        (invariant 6): what the user thought in March is a fact, and changing their mind in September is a
        second fact, not a correction of the first. **Verdict resolution** is what makes the later one the
        one that counts.

        `batch_id` is `NULL` on the entry, which is how an edit made from **History** is told apart from
        one given to a **Batch** — the only distinction the log draws between them.

        **Ignore** is choosable, since #37 (ADR 0015): it is how **History** withdraws a **Verdict**, the
        same entry unmarking a tile writes, so the two screens mean one thing by one action. It is the
        latest entry, so it overturns whatever stood. Un-**Banning** falls out of this rather than being
        built: a **Batch** excludes by *resolved* **Verdict**, and a **Ban** overturned by an **Ignore** is
        no longer one. Nothing writes a **Clearance** any more.

        An unknown **Wallpaper** is refused rather than left to the foreign key, so the caller gets a reason
        instead of an `IntegrityError`.

        The **Library** is reconciled afterwards, outside the transaction and for the same reasons
        `submit_batch` does it there (ADR 0006): a new **Favourite** gains a file and a replaced one loses
        the file wallpapi actually wrote.
        """
        recorded_at = self._clock.now().isoformat()
        with self._write() as write:
            if not _wallpaper_exists(write, wallpaper_id):
                return HistoryRefused(reason=HistoryRefused.Reason.UNKNOWN_WALLPAPER)
            write.execute(_APPEND_HISTORY_ENTRY, (wallpaper_id, verdict.value, recorded_at))
        self.reconcile_library()
        return None

    def list_history_rows(self, *, verdict: Verdict | None = None, page: int = 1) -> HistoryPage:
        """One page of **History**: the **Wallpapers** with entries, newest activity first.

        A view over the **Decision log** and not a second store — the same CTE **Verdict resolution** uses,
        with the paging and the filter in SQL. It has to be in SQL: a **History** of thousands of
        **Ignores** cannot be resolved in Python and then sliced, and the filter is over the *resolved*
        **Verdict** rather than over anything a row holds.

        `verdict` narrows to one resolved **Verdict**, **Ignore** included, which is what keeps the
        thousands of **Ignores** off the page a user is actually looking for something on. `page` is
        clamped into range rather than refused: a stale link to page 9 of a **History** that has shrunk
        should show the last page, not an error.
        """
        parameters: list[str] = [] if verdict is None else [verdict.value]
        filtered = verdict is not None
        connection = self._connect()
        total = int(connection.execute(_history_count_query(filtered=filtered), parameters).fetchone()[0])
        pages = max(1, -(-total // HISTORY_PAGE_SIZE))
        wanted = min(max(page, 1), pages)
        rows = connection.execute(
            _history_rows_query(filtered=filtered),
            [*parameters, HISTORY_PAGE_SIZE, (wanted - 1) * HISTORY_PAGE_SIZE],
        ).fetchall()
        return HistoryPage(
            rows=self._history_rows_from(rows), page=wanted, pages=pages, total=total, verdict=verdict
        )

    def get_history_row(self, wallpaper_id: str) -> HistoryRow | None:
        """One **Wallpaper**'s **History** row, or `None` if it has no entries.

        What an edit swaps back into the page. Built by the same query as the listing,
        so the row the user is left looking at is the row a reload would give them.
        """
        row = self._connect().execute(_HISTORY_ROW, (wallpaper_id,)).fetchone()
        if row is None:
            return None
        return self._history_rows_from([row])[0]

    def _history_rows_from(self, rows: Sequence[sqlite3.Row]) -> tuple[HistoryRow, ...]:
        """Turn listing rows into **History** rows, resolving the page's **Wallpapers** in one call.

        The values come from `resolve_verdicts` rather than from the listing query even though the query
        already chose each **Verdict**: the query is what the filter and the ordering need, and what a
        resolved **Verdict** is *worth* belongs to the one operation #9 will be summing across a **Pool**.
        Both read the same CTE, so they cannot disagree about which **Verdict** it is.
        """
        resolved = self.resolve_verdicts([str(row["id"]) for row in rows])
        return tuple(
            HistoryRow(
                wallpaper=_wallpaper_from_row(row),
                resolved=resolved[str(row["id"])],
                latest_at=dt.datetime.fromisoformat(str(row["latest_at"])),
            )
            for row in rows
        )

    def list_history(
        self, *, batch_id: str | None = None, wallpaper_id: str | None = None
    ) -> list[DecisionEntry]:
        """The **Decision log**'s raw entries in sequence order — the order resolution depends on.

        `batch_id` narrows it to one submission and `wallpaper_id` to one **Wallpaper**. The **History**
        *page* is `list_history_rows`; this stays the entry-by-entry view, which is what shows that an edit
        appended rather than rewrote.

        Entries are **Verdicts** or **Clearances**, which is why `DecisionEntry.entry` is neither named nor
        typed as a **Verdict** alone.
        """
        conditions: list[str] = []
        parameters: list[str] = []
        if batch_id is not None:
            conditions.append("batch_id = ?")
            parameters.append(batch_id)
        if wallpaper_id is not None:
            conditions.append("wallpaper_id = ?")
            parameters.append(wallpaper_id)
        query = "SELECT seq, wallpaper_id, batch_id, verdict, recorded_at FROM decision_log"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        rows = self._connect().execute(f"{query} ORDER BY seq", parameters).fetchall()
        return [
            DecisionEntry(
                seq=int(row["seq"]),
                wallpaper_id=str(row["wallpaper_id"]),
                batch_id=None if row["batch_id"] is None else str(row["batch_id"]),
                entry=_entry_from(row["verdict"]),
                recorded_at=dt.datetime.fromisoformat(str(row["recorded_at"])),
            )
            for row in rows
        ]


def _alternated(last: RefillStrategy | None, *, has_favourites: bool) -> RefillStrategy:
    """Whichever strategy did not take the last step, while both have work to do.

    Strict turns rather than a share of the budget or a schedule: with one **API call** per step, "take
    turns" *is* an even split, and there is no second number for anyone to get wrong. Without a
    **Favourite** there is no like: search to make, so every step is random — and because the last
    strategy is still recorded, the first step after something is **Favourited** is the like: one.
    """
    if not has_favourites or last is RefillStrategy.LIKE:
        return RefillStrategy.RANDOM
    return RefillStrategy.RANDOM if last is None else RefillStrategy.LIKE


def _validated_batch_size(value: int | str) -> int | SettingsRefused.Reason:
    """The batch size, or the reason it is not one.

    Coerces from text because the form posts text. `int` rather than `float`: "1.5" is refused outright
    rather than silently becoming a **Batch** of one, because a size the user did not ask for is worse
    than being told to type a whole number.
    """
    try:
        size = int(str(value).strip())
    except ValueError:
        return SettingsRefused.Reason.BATCH_SIZE_NOT_A_NUMBER
    if not MIN_BATCH_SIZE <= size <= MAX_BATCH_SIZE:
        return SettingsRefused.Reason.BATCH_SIZE_OUT_OF_RANGE
    return size


def _passes_filters(wallpaper: Wallpaper, settings: Settings) -> bool:
    """Every **Filter**, checked locally.

    The same rule admits a **Wallpaper** to the **Pool** and decides whether one already in it has to
    leave, so a **Filter** cannot mean two different things at the two ends.

    Purity is checked here as well as being fixed in the query. `atleast` and `ratios` are checked here as
    well as being sent. The API is trusted but not relied upon: a parameter that was silently ignored, or a
    response shaped differently from the documentation, must not be able to put a NSFW or 1024x768
    **Wallpaper** in front of somebody.
    """
    return (
        wallpaper.purity.strip().lower() == SFW_PURITY_NAME
        and wallpaper.width >= settings.min_width
        and wallpaper.height >= settings.min_height
        and wallpaper.favourites >= settings.min_favourites
        and _matches_an_allowed_ratio(wallpaper, settings.allowed_ratios)
    )


def _matches_an_allowed_ratio(wallpaper: Wallpaper, allowed: Sequence[str]) -> bool:
    """Whether the **Wallpaper**'s own shape is within `RATIO_TOLERANCE` of any allowed ratio.

    Computed from the width and the height rather than read from Wallhaven's `ratio` field, which is
    rounded to two places, and matched with a tolerance because Wallhaven's `ratios=` buckets rather than
    matching exactly — a 3440x1440 ultrawide is 2.39 and it is served under `21x9`.
    """
    if wallpaper.height <= 0:
        return False
    shape = wallpaper.width / wallpaper.height
    return any(
        abs(shape - named) <= RATIO_TOLERANCE
        for named in (_ratio_value(ratio) for ratio in allowed)
        if named is not None
    )


def _ratio_value(named: str) -> float | None:
    """`"16x9"` as 1.777…, or `None` if it is not a ratio at all.

    Tolerant of nonsense rather than raising: the settings validator is what refuses an unknown ratio, and
    this one is also reached with whatever a hand-edited row happens to hold.
    """
    width, _, height = named.partition("x")
    try:
        return int(width) / int(height)
    except ValueError, ZeroDivisionError:
        return None


def _whole_number(value: object) -> int | None:
    """The value as a whole number, or `None` if it is not one.

    `int` and never `float`: "1.5" and "1e3" are refused outright rather than silently becoming something
    the user did not type. The form posts text, so the coercion belongs with the rule.
    """
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _validated_pool_target_size(value: int | str) -> int | SettingsRefused.Reason:
    """The **Pool** target size, or the reason it is not one."""
    size = _whole_number(value)
    if size is None or not MIN_POOL_TARGET_SIZE <= size <= MAX_POOL_TARGET_SIZE:
        return SettingsRefused.Reason.POOL_TARGET_SIZE_INVALID
    return size


def _validated_min_width(value: int | str) -> int | SettingsRefused.Reason:
    return _validated_pixels(value, SettingsRefused.Reason.MIN_WIDTH_INVALID)


def _validated_min_height(value: int | str) -> int | SettingsRefused.Reason:
    return _validated_pixels(value, SettingsRefused.Reason.MIN_HEIGHT_INVALID)


def _validated_pixels(value: int | str, reason: SettingsRefused.Reason) -> int | SettingsRefused.Reason:
    """One axis of the minimum resolution **Filter**, or the reason it is not one.

    Zero is accepted: "no minimum on this axis" is a real answer, and turning a **Filter** off by emptying
    it is better than a second setting saying whether it is on.
    """
    pixels = _whole_number(value)
    if pixels is None or not 0 <= pixels <= MAX_FILTER_PIXELS:
        return reason
    return pixels


def _validated_min_favourites(value: int | str) -> int | SettingsRefused.Reason:
    """The minimum **Favourites** **Filter**, or the reason it is not one.

    No ceiling: unlike a resolution there is no number above which this can only be a typo, and its effect
    — a **Pool** that fills slowly or not at all — is visible on the **Batch** page's refill indicator.
    """
    favourites = _whole_number(value)
    if favourites is None or favourites < 0:
        return SettingsRefused.Reason.MIN_FAVOURITES_INVALID
    return favourites


def _decimal_number(value: object) -> float | None:
    """The value as a finite decimal number, or `None` if it is not one.

    `float` here where every other setting is an `int`, because a radius and a decay are genuinely
    fractional — 0.5 and 4.0 are the defaults, and rounding either to a whole number would make half the
    usable range unreachable. Infinities and NaN are refused: `float("nan")` parses, and a NaN radius would
    silently make every comparison against it false and every **Wallpaper** an **Unknown**.
    """
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _validated_similarity_radius(value: float | str) -> float | SettingsRefused.Reason:
    """The similarity radius, or the reason it is not one.

    Bounded to `[0, 1]` because it is compared against `1 - similarity`, and a similarity is in `[0, 1]`:
    above 1 nothing is ever outside the radius, so the setting would silently stop doing anything. Zero is
    accepted and means "only a **Wallpaper** identical to this one counts".
    """
    number = _decimal_number(value)
    if number is None or not 0.0 <= number <= 1.0:
        return SettingsRefused.Reason.SIMILARITY_RADIUS_INVALID
    return number


def _validated_similarity_decay(value: float | str) -> float | SettingsRefused.Reason:
    """The similarity decay, or the reason it is not one.

    Non-negative: a negative decay would make distant **Wallpapers** count for *more* than near ones, which
    is not a setting, it is the rule inverted. Capped at `MAX_SIMILARITY_DECAY`.
    """
    number = _decimal_number(value)
    if number is None or not 0.0 <= number <= MAX_SIMILARITY_DECAY:
        return SettingsRefused.Reason.SIMILARITY_DECAY_INVALID
    return number


def _validated_thumbnail_cache_max_mb(value: int | str) -> int | SettingsRefused.Reason:
    """The **Thumbnail cache** size cap in megabytes, or the reason it is not one.

    Zero is accepted and means what it says: keep no thumbnail the cap is allowed to touch. It is not the
    same as keeping none at all — a **Wallpaper** with an **Explicit Verdict** is never evicted (invariant
    8), so **History** still renders — and every evicted tile is re-fetched the next time it is asked for.

    No ceiling. Unlike a resolution there is no number above which this can only be a typo, and a cap set
    higher than the disk simply never fires, which is what "no cap" means anyway.
    """
    megabytes = _whole_number(value)
    if megabytes is None or megabytes < 0:
        return SettingsRefused.Reason.THUMBNAIL_CACHE_MAX_MB_INVALID
    return megabytes


def _validated_allowed_ratios(value: Sequence[str] | str) -> tuple[str, ...] | SettingsRefused.Reason:
    """The allowed ratios, or the reason they are not.

    Accepted as the comma-separated string the form posts or as a sequence, trimmed and de-duplicated with
    the order kept. Checked against `WALLHAVEN_RATIOS` rather than against being a string, because an
    unrecognised `ratios=` value is not an error Wallhaven reports — it is a search that quietly returns
    something other than what was asked for.

    An empty list is refused: "every ratio" is written as the full list, because a blank field is far more
    likely to be a mistake than an intention.
    """
    parts = value.split(",") if isinstance(value, str) else list(value)
    named = list(dict.fromkeys(part.strip() for part in parts))
    if not named or any(part not in WALLHAVEN_RATIOS for part in named):
        return SettingsRefused.Reason.ALLOWED_RATIOS_INVALID
    return tuple(named)


def _validated_library_path(value: Path | str) -> Path | SettingsRefused.Reason:
    """The **Library** path, or the reason it is not one.

    Absolute or nothing (invariant 9): a relative path moves the **Library** with the working directory,
    and every absolute path recorded for a file written into it would then point somewhere that cannot be
    found again. Nothing here touches the filesystem — the path is allowed not to exist yet, and the
    **Library** writer creates it on its first write (#5).
    """
    text = str(value).strip()
    if not text:
        return SettingsRefused.Reason.LIBRARY_PATH_EMPTY
    path = Path(text)
    if not path.is_absolute():
        return SettingsRefused.Reason.LIBRARY_PATH_NOT_ABSOLUTE
    return path


LIBRARY_FILE_NAME = re.compile(r"[A-Za-z0-9]+\.[a-z0-9]{1,5}")
"""The only shape a **Library** file name may take: a Wallhaven ID, a dot, and one short extension.

Deliberately narrower than "a legal file name". Everything wallpapi puts in the **Library** is named by
this rule, so anything that does not fit it is not something wallpapi wrote — and a deletion is only ever
as safe as the worst path it is willing to consider. A name is a *name*: `..`, a separator, a space and a
second dot are all refused, which is what keeps a hand-edited row or a hostile `full_url` from becoming a
path at all. ASCII by construction rather than by `str.isalnum`, which counts `²` as alphanumeric.
"""

DEFAULT_LIBRARY_SUFFIX = ".jpg"
"""What a `full_url` that does not end in a recognisable extension is saved as.

Wallhaven serves JPEG for all but a handful of **Wallpapers**, and the bytes are a wallpaper whatever the
URL calls itself — so this is a fallback rather than a refusal.
"""


def library_file_name(wallpaper_id: str, source_url: str) -> str | None:
    """What a **Library** file is called, or `None` if this **Wallpaper** cannot have one.

    The naming rule lives with the Core service because the Core service is what records the result
    (ADR 0006). `None` rather than a raise, because it is the same answer as "that destination is not
    inside the **Library**": the **Favourite** stands and simply has no file, which the next
    reconciliation will try again and reach the same conclusion about.

    A suffix that does not fit `LIBRARY_FILE_NAME` falls back to `.jpg`; a **Wallhaven** ID that does not
    fit names nothing at all. The asymmetry is deliberate — the suffix is Wallhaven's to get wrong and the
    ID is what the file is *for*.
    """
    named = f"{wallpaper_id}{_url_suffix(source_url, default=DEFAULT_LIBRARY_SUFFIX)}"
    if LIBRARY_FILE_NAME.fullmatch(named):
        return named
    fallback = f"{wallpaper_id}{DEFAULT_LIBRARY_SUFFIX}"
    return fallback if LIBRARY_FILE_NAME.fullmatch(fallback) else None


def confined_to_library(path: Path, library_root: Path) -> Path | None:
    """The resolved `path`, if wallpapi may write it or delete it — otherwise `None`.

    **The one guard, and both the write and the delete go through it** (invariant 9). Deleting a file
    wallpapi wrote is acceptable only while it is guaranteed that the only folder wallpapi can reach is
    the **Library** folder the user chose; a **Wallpaper** named `~/../../../Windows/system32` must not be
    able to make that untrue.

    Three conditions, and all three are about the path rather than about the file on the end of it:

    - the name fits `LIBRARY_FILE_NAME`;
    - the path, fully resolved — `..` collapsed, every symlink and junction followed — lies inside the
      fully resolved **Library** folder;
    - it is not the **Library** folder itself.

    Resolution is what makes the second condition mean anything: a symlink sitting in the **Library** and
    pointing at `C:\\Windows` is inside the folder by every syntactic measure and outside it by the only
    one that matters. Both sides are resolved, so a **Library path** that itself goes through a link is
    not refused for doing so. Compared through `os.path.normcase` and by path component: NTFS is
    case-insensitive, and a string prefix would put `Library2` inside `Library`.

    **The resolved path is returned, not a yes or no**, and it is the path callers must use. A **Library**
    write is a temp file plus an `os.replace` beside its destination (invariant 10), so checking one path
    and writing another would leave exactly the gap this exists to close.

    `strict=False` because the **Library** folder is allowed not to exist yet: #4 stores the setting
    without touching the filesystem and the first **Favourite** is what creates the folder.
    """
    try:
        resolved = path.resolve(strict=False)
        root = library_root.resolve(strict=False)
    except OSError, ValueError:
        # A recorded path SQLite handed back can hold anything a hand-edited row can hold, including
        # characters Windows will not even parse. Unresolvable is refused, like everything else odd.
        return None
    if not LIBRARY_FILE_NAME.fullmatch(resolved.name):
        return None
    here = os.path.normcase(str(resolved))
    there = os.path.normcase(str(root))
    if here == there:
        return None
    try:
        if os.path.commonpath((here, there)) != there:
            return None
    except ValueError:
        # Different drives, or one of the two not absolute. Either way there is no "inside" to be in.
        return None
    return resolved


def validated_mix(
    name: str, *, unknown: int | str, banger: int | str, dud: int | str
) -> Mix | SettingsRefused.Reason:
    """A **Mix**, or the reason those numbers are not one.

    **The one place that decides what a Mix is.** Three callers, and they must not be allowed to disagree:
    `list_mixes` reads rows that could have been hand-edited, the tests that pin the rule call it directly,
    and `save_mix` calls it with whatever the user typed into the form (#12). A rule enforced in the
    dataclass instead would turn "the user typed 30/30/30" into a traceback, and a rule enforced only at
    the form would let a hand-edited row into the draw.

    Whole percentages summing to exactly `MIX_TOTAL`, none of them negative. Exactly, because the slack in
    an **Allocation** is the leftover roll: a **Mix** summing to 99 would leave one slot rolled on every
    **Batch** whatever the size, and one summing to 101 would allocate a slot the **Batch** does not have.
    A **Zone** at zero per cent is allowed and means it — **Refine** without any **Duds** at all is a
    **Mix** somebody may well want.

    A name is what is left after trimming, is not empty, and is no longer than `MAX_MIX_NAME_LENGTH`.
    Trimmed because the surrounding whitespace is the form's rather than the user's; compared
    case-sensitively everywhere else, because "Night" and "night" are two things the user typed and this
    is not the place to decide they meant one.

    Accepted as text as well as typed, for the reason every other validator here is: the form posts text,
    SQLite hands back whatever the column holds, and the coercion rule belongs with the rule.
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


_UPSERT_SETTING = """
INSERT INTO settings (key, value) VALUES (?, ?)
ON CONFLICT (key) DO UPDATE SET value = excluded.value
"""

_UPSERT_MIX = """
INSERT INTO mixes (name, unknown, banger, dud) VALUES (?, ?, ?, ?)
ON CONFLICT (name) DO UPDATE
SET unknown = excluded.unknown, banger = excluded.banger, dud = excluded.dud
"""
"""One statement for creating a **Mix** and for editing one (#12).

The name is the key, so "save this **Mix**" is the same write either way and the form does not have to
know which it is doing. It also means there is no rename: saving **Explore**'s percentages under a new
name makes a second **Mix** rather than moving the first, which is the honest reading of a form whose
name field is what identifies the row.
"""

_DELETE_MIX = "DELETE FROM mixes WHERE name = ?"

_LIBRARY_CANDIDATES = """
SELECT w.id AS wallpaper_id, w.full_url AS full_url, f.path AS path
FROM wallpapers AS w
LEFT JOIN library_files AS f ON f.wallpaper_id = w.id
WHERE f.wallpaper_id IS NOT NULL
   OR w.id IN (SELECT wallpaper_id FROM decision_log WHERE verdict = ?)
ORDER BY w.id
"""
"""Everything a reconciliation could possibly have to do something about, and nothing else.

Two halves, because the work has two directions. A **Wallpaper** can only *resolve* to **Favourite** if it
was **Favourited** at least once, so the **Decision log** half is a superset of what might need writing —
**Verdict resolution** then decides which of them still count. The `library_files` half is everything that
might need deleting. Resolution is not expressed in SQL here: the rule lives in `resolve_verdicts` and
having it in two places is how the two would come to disagree.

Ordered so that what a reconciliation reports is in a fixed order rather than whatever the query planner
felt like — nothing depends on the order of the work itself.
"""

_FAVOURITED_AT_LEAST_ONCE = """
SELECT DISTINCT wallpaper_id FROM decision_log WHERE verdict = ? ORDER BY wallpaper_id
"""
"""Every **Wallpaper** that could possibly resolve to **Favourite**, and nothing else.

A superset, not an answer: **Verdict resolution** decides which of them still count, and it lives in
`resolve_verdicts` rather than being written a second time in SQL here — two spellings of one rule is how
the two come to disagree. Same shape, and the same reasoning, as `_LIBRARY_CANDIDATES`.
"""

_RECORD_LIBRARY_FILE = """
INSERT INTO library_files (wallpaper_id, path, written_at) VALUES (?, ?, ?)
ON CONFLICT (wallpaper_id) DO UPDATE SET path = excluded.path, written_at = excluded.written_at
"""
"""An upsert rather than an insert.

A write only happens when there is no row — the removal that precedes a re-**Favourite** deletes it — so
the conflict branch is unreachable as things stand. It is here because the alternative to reaching it is an
`IntegrityError` that would be counted as a failed write and retried for ever, and because the row's job is
to say where the file is: the last write wins is the only answer that can be right.
"""

_ABSENT = ResolvedVerdict(verdict=None, value=0)
"""A **Wallpaper** with no **Decision log** entries at all."""

_EXPLICIT_VALUES = {Verdict.FAVOURITE: 100, Verdict.LIKE: 50, Verdict.BAN: -100}
"""What each **Explicit Verdict** resolves to. An **Ignore** is not here — it is `_IGNORE_VALUE`."""

_IGNORE_VALUE = -10
"""What a resolved **Ignore** is worth. Once, never stacked (#37)."""


def _entry_from(stored: object) -> Verdict | Clearance:
    """One `decision_log.verdict` cell as the entry it is.

    The column holds **Clearances** as well as **Verdicts** (#7), so `Verdict(cell)` is wrong on its own —
    it raises on `'cleared'`. This is the one place that reads the column into a Python value, so it is the
    one place that has to know.
    """
    text = str(stored)
    return Clearance.CLEARED if text == Clearance.CLEARED.value else Verdict(text)


def _is_explicit(resolved: ResolvedVerdict) -> bool:
    """Whether what stands against a **Wallpaper** is an **Explicit Verdict** — the question pre-marking a
    reshown tile asks (ADR 0015), of an already-resolved answer rather than of the **Decision log**.

    Present and not an **Ignore**, which is the whole rule. Everything difficult about it has already
    happened in `_RESOLUTION_CTE`: an **Explicit Verdict** overturned by a later **Ignore** resolves to the
    **Ignore** and is not explicit (#37), and a legacy **Clearance** resolves to nothing at all. A **Ban**
    would answer `True` and never be asked — `classify_pool` leaves **Banned** **Wallpapers** out of the
    draw entirely.
    """
    return resolved.verdict is not None and resolved.verdict is not Verdict.IGNORE


def _resolved_from(resolved: object) -> ResolvedVerdict:
    """What a resolved **Verdict** is worth. The rule that *chose* it is `_RESOLUTION_CTE`.

    Deliberately not a second copy of the rule. The **History** listing has to filter by resolved
    **Verdict** in SQL — a page of 100 rows cannot be sliced out of a log resolved in Python — so the rule
    has to exist in SQL, and the moment it exists in two places is the moment they disagree. So SQL chooses
    the **Verdict** and this turns it into a number, which is the half SQL has no business knowing.

    An **Ignore** is worth `_IGNORE_VALUE` once, however many came before it (#37): passing a **Wallpaper**
    over ten times is not ten times the dislike of a **Dud** seen once.
    """
    if resolved is None:
        return _ABSENT
    verdict = Verdict(str(resolved))
    if verdict is Verdict.IGNORE:
        return ResolvedVerdict(verdict=verdict, value=_IGNORE_VALUE)
    return ResolvedVerdict(verdict=verdict, value=_EXPLICIT_VALUES[verdict])


_RESOLUTION_CTE = f"""
WITH entries AS (
    SELECT wallpaper_id, MAX(seq) AS latest_seq
    FROM decision_log
    {{restriction}}
    GROUP BY wallpaper_id
),
resolution AS (
    SELECT
        entries.wallpaper_id,
        entries.latest_seq,
        CASE WHEN latest.verdict != '{Clearance.CLEARED.value}' THEN latest.verdict END AS resolved
    FROM entries
    JOIN decision_log AS latest ON latest.seq = entries.latest_seq
)
"""
"""**Verdict resolution**, in one place, as the prelude to every query that needs it.

The rule since #37 (ADR 0015): the latest entry decides, whatever it is, and nothing before it counts. An
**Ignore** after an **Explicit Verdict** overturns it — the batch page pre-marks a reshown **Wallpaper**
with its standing **Verdict**, so that **Ignore** can only be the user unmarking it — and an **Explicit
Verdict** after an **Ignore** overturns the **Ignore**. A legacy **Clearance**, which nothing writes any
more, resolves to `NULL` when it is the latest entry: the same answer as never having seen the
**Wallpaper**.

`MAX(seq)` and never `recorded_at` (invariant 4): every entry from one submit transaction shares a
timestamp, and two **History** edits in the same frozen second are exactly the case that has no answer
without the sequence.

`{{restriction}}` is a `WHERE` over `decision_log` that narrows the aggregate to the **Wallpapers** a
caller cares about, or empty for the whole log. It is always a literal built here — never anything a caller
supplies — with the values themselves bound as parameters.
"""


def _resolution_query(select: str, *, restriction: str = "") -> str:
    """A query over the resolved **Decision log**: the shared CTE, then whatever the caller selects."""
    return _RESOLUTION_CTE.format(restriction=restriction) + select


_HISTORY_SELECT = """
SELECT w.*, resolution.resolved AS resolved, latest.recorded_at AS latest_at
FROM resolution
JOIN wallpapers AS w ON w.id = resolution.wallpaper_id
JOIN decision_log AS latest ON latest.seq = resolution.latest_seq
"""
"""One **History** row per **Wallpaper** with entries: the image, what it resolves to, and when it was
last judged.

`latest` is joined on `latest_seq` rather than the timestamp being aggregated with a `MAX`, so the
`recorded_at` shown is the one belonging to the entry that actually decided the order (invariant 4).
"""


def _history_rows_query(*, filtered: bool) -> str:
    """The **History** listing, optionally narrowed to one resolved **Verdict**.

    Ordered by `latest_seq` and never by `latest_at`: "newest activity first" has to be a total order, and
    a whole submitted **Batch** shares one timestamp.
    """
    where = "WHERE resolution.resolved = ?" if filtered else ""
    return _resolution_query(
        f"{_HISTORY_SELECT}{where}\nORDER BY resolution.latest_seq DESC\nLIMIT ? OFFSET ?"
    )


def _history_count_query(*, filtered: bool) -> str:
    """How many rows the same filter matches, for the paging."""
    where = "WHERE resolved = ?" if filtered else ""
    return _resolution_query(f"SELECT COUNT(*) FROM resolution {where}")


_HISTORY_ROW = _resolution_query(_HISTORY_SELECT, restriction="WHERE wallpaper_id = ?")
"""One **Wallpaper**'s **History** row — what an edit swaps back into the page."""

_EXPLICITLY_DECIDED = _resolution_query(
    f"SELECT wallpaper_id FROM resolution WHERE resolved IS NOT NULL AND resolved != '{Verdict.IGNORE.value}'"
)
"""Every **Wallpaper** whose resolved **Verdict** is an **Explicit Verdict** — the ones whose thumbnails
eviction may never touch (invariant 8)."""

_AWAITING_A_VERDICT = """
SELECT wallpaper_id FROM pool
UNION
SELECT bw.wallpaper_id
FROM batch_wallpapers AS bw
JOIN batches AS b ON b.id = bw.batch_id
WHERE b.submitted_at IS NULL
"""
"""Every **Wallpaper** still to be shown: the **Pool**, plus the live **Batch** in case a **Filter** change
has pruned one of its **Wallpapers** out of the **Pool** while it is on screen."""

_APPEND_HISTORY_ENTRY = """
INSERT INTO decision_log (wallpaper_id, batch_id, verdict, recorded_at) VALUES (?, NULL, ?, ?)
"""
"""An edit made from **History**, an **Ignore** included.

`batch_id` is `NULL` because there is no **Batch**: the user was looking at a list, not judging a screenful.
That null is the only thing in the log that distinguishes the two, and `list_history(batch_id=...)`
depends on it — a **History** edit must not be counted among what a **Batch** recorded.
"""


def _wallpaper_exists(connection: sqlite3.Connection, wallpaper_id: str) -> bool:
    """Whether this database has ever seen the **Wallpaper**.

    Checked before appending rather than left to `decision_log`'s foreign key, so a caller gets a refusal
    in the house style instead of an `IntegrityError` out of the middle of a transaction.
    """
    return connection.execute("SELECT 1 FROM wallpapers WHERE id = ?", (wallpaper_id,)).fetchone() is not None


def _cached_thumbnails(directory: Path) -> list[_CachedThumbnail]:
    """Every file in the **Thumbnail cache**, stat-ed once, keyed by the **Wallpaper** its name carries.

    The name is the whole mapping — `get_thumbnail` writes `{wallhaven_id}{suffix}` — so a file whose stem
    matches no `wallpapers` row belongs to no **Wallpaper** and is evicted by the first pass for free.

    A file that vanishes between the listing and the stat is skipped rather than raising: this runs after
    a **Batch** has already been recorded, and nothing here may turn that into an error.
    """
    cached: list[_CachedThumbnail] = []
    for path in sorted(directory.iterdir()):
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            cached.append(
                _CachedThumbnail(
                    wallpaper_id=path.stem, path=path, size=stat.st_size, modified_at=stat.st_mtime
                )
            )
    return cached


_NOTHING_EVICTED = ThumbnailEviction(evicted=(), remaining_bytes=0, over_cap=False)
"""An empty or absent **Thumbnail cache**: nothing to delete and nothing taking up room."""


def _load_live_batch(connection: sqlite3.Connection) -> Batch | None:
    """The most recent unsubmitted **Batch**, rebuilt from storage, or `None`."""
    batch = connection.execute(
        "SELECT id, created_at, size FROM batches WHERE submitted_at IS NULL ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if batch is None:
        return None
    rows = connection.execute(_SELECT_BATCH_WALLPAPERS, (batch["id"],)).fetchall()
    return Batch(
        id=str(batch["id"]),
        size=int(batch["size"]),
        created_at=dt.datetime.fromisoformat(str(batch["created_at"])),
        wallpapers=tuple(_wallpaper_from_row(row) for row in rows),
        drafts=_load_drafts(connection, str(batch["id"])),
        # Only the rows that have one. A **Batch** minted before migration 6 has NULL here, and the tile
        # renders without a label rather than being given a **Zone** nobody classified it into.
        zones={str(row["id"]): Zone(str(row["zone"])) for row in rows if row["zone"] is not None},
    )


def _load_drafts(connection: sqlite3.Connection, batch_id: str) -> dict[str, Verdict]:
    """The **Draft Batch**: only the **Wallpapers** actually marked, because absence means **Ignore**."""
    rows = connection.execute(
        "SELECT wallpaper_id, verdict FROM draft_batch WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    return {str(row["wallpaper_id"]): Verdict(str(row["verdict"])) for row in rows}


def _wallpaper_from_row(row: sqlite3.Row) -> Wallpaper:
    return Wallpaper(
        id=str(row["id"]),
        width=int(row["width"]),
        height=int(row["height"]),
        ratio=str(row["ratio"]),
        category=str(row["category"]),
        purity=str(row["purity"]),
        favourites=int(row["favourites"]),
        colours=tuple(str(row["colours"]).split(",")) if row["colours"] else (),
        thumbnail_url=str(row["thumbnail_url"]),
        full_url=str(row["full_url"]),
        page_url=str(row["page_url"]),
    )


def _url_suffix(url: str, *, default: str = ".jpg") -> str:
    """The file extension of a URL's path, ignoring any query string."""
    return PurePosixPath(urlsplit(url).path).suffix or default


def _distinct(wallpapers: Sequence[Wallpaper]) -> list[Wallpaper]:
    """Distinct **Wallpapers** by Wallhaven ID, first occurrence winning.

    A **Batch** is `size` distinct **Wallpapers**, not `size` rows: a random search can return the same one
    more than once.
    """
    seen: set[str] = set()
    unique: list[Wallpaper] = []
    for wallpaper in wallpapers:
        if wallpaper.id not in seen:
            seen.add(wallpaper.id)
            unique.append(wallpaper)
    return unique


def _wallpaper_row(wallpaper: Wallpaper) -> tuple[str | int, ...]:
    return (
        wallpaper.id,
        wallpaper.width,
        wallpaper.height,
        wallpaper.ratio,
        wallpaper.category,
        wallpaper.purity,
        wallpaper.favourites,
        ",".join(wallpaper.colours),
        wallpaper.thumbnail_url,
        wallpaper.full_url,
        wallpaper.page_url,
    )


_ADMIT_TO_POOL = """
INSERT INTO pool (wallpaper_id, fetched_at, source)
SELECT :id, :fetched_at, :source
WHERE NOT EXISTS (SELECT 1 FROM decision_log WHERE wallpaper_id = :id)
ON CONFLICT (wallpaper_id) DO NOTHING
"""
"""`DO NOTHING` rather than an upsert: a **Wallpaper** the refill meets again is already in the **Pool**,
and `fetched_at` should stay the moment it first arrived.

**Nothing the Decision log mentions is admitted (#38, ADR 0016).** Any entry at all, a legacy `cleared`
one included: it was decided, whatever it resolves to. Asked of the log itself rather than of resolution,
because the question is "has it ever been decided", which no resolved value answers — a **Clearance**
resolves to the same nothing a **Wallpaper** never seen does."""

_SELECT_POOL_WALLPAPERS = """
SELECT w.*
FROM pool
JOIN wallpapers AS w ON w.id = pool.wallpaper_id
ORDER BY pool.rowid
"""
"""The whole **Pool**, in a fixed order.

Ordered because the sample is drawn by the seeded random source: without an `ORDER BY`, SQLite's row order
is an implementation detail and the same seed over the same **Pool** could produce a different **Batch**.
"""

_SELECT_POOL_THUMBNAILS = """
SELECT w.id, w.thumbnail_url
FROM pool
JOIN wallpapers AS w ON w.id = pool.wallpaper_id
ORDER BY pool.wallpaper_id
"""
"""Every **Pool** member's thumbnail URL, in the order the downloader fetches them (#44).

By `wallpaper_id`, the primary key, so the order is deterministic and needs no index of its own.
"""

_SELECT_DECIDED_WALLPAPERS = """
SELECT w.*
FROM wallpapers AS w
WHERE w.id IN (SELECT wallpaper_id FROM decision_log)
ORDER BY w.id
"""
"""Every **Wallpaper** the **Decision log** mentions, whether or not it is still in the **Pool**.

The whole row and not just the ID, because the **Similarity provider** is handed **Wallpapers**: it reads
their colours and their category, and a provider that had to fetch them itself would be a second seam into
storage. Ordered so that the matrix's columns — and so every **Score** — are the same from one call to the
next, which is what makes a classification reproducible.

Resolution is not expressed here. Which of these actually count is `resolve_verdicts`' answer, and having
the rule in two places is how the two would come to disagree.
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
"""Select-all as one statement: the **Draft Batch** is built from the rows the **Batch** shows.

Reading `batch_wallpapers` inside the same transaction as the delete is what makes the rewrite atomic. A
Python round trip per **Wallpaper** would be 32 statements for one click and would have to be handed a
list of IDs the caller had read separately, outside the write lock.
"""

_UPSERT_WALLPAPER = """
INSERT INTO wallpapers
    (id, width, height, ratio, category, purity, favourites, colours, thumbnail_url, full_url, page_url)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
    width = excluded.width,
    height = excluded.height,
    ratio = excluded.ratio,
    category = excluded.category,
    purity = excluded.purity,
    favourites = excluded.favourites,
    colours = excluded.colours,
    thumbnail_url = excluded.thumbnail_url,
    full_url = excluded.full_url,
    page_url = excluded.page_url
"""

_MIGRATION_1 = (
    """
CREATE TABLE wallpapers (
    id            TEXT PRIMARY KEY,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    ratio         TEXT NOT NULL,
    category      TEXT NOT NULL,
    purity        TEXT NOT NULL,
    favourites    INTEGER NOT NULL,
    colours       TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL,
    full_url      TEXT NOT NULL,
    page_url      TEXT NOT NULL
)
""",
    """
CREATE TABLE batches (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    size         INTEGER NOT NULL,
    submitted_at TEXT
)
""",
    """
CREATE TABLE batch_wallpapers (
    batch_id     TEXT NOT NULL REFERENCES batches (id),
    wallpaper_id TEXT NOT NULL REFERENCES wallpapers (id),
    position     INTEGER NOT NULL,
    PRIMARY KEY (batch_id, wallpaper_id)
)
""",
    """
CREATE TABLE decision_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallpaper_id TEXT NOT NULL REFERENCES wallpapers (id),
    batch_id     TEXT REFERENCES batches (id),
    verdict      TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
)
""",
    """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""",
    "INSERT INTO settings (key, value) VALUES ('batch_size', '8')",
)
"""Migration 1, one statement per entry.

Not an `executescript`: that commits any transaction already open before it runs, which would take the
migration out of the `BEGIN IMMEDIATE` it is supposed to be inside.
"""


_MIGRATION_2 = (
    """
CREATE TABLE draft_batch (
    batch_id     TEXT NOT NULL REFERENCES batches (id),
    wallpaper_id TEXT NOT NULL REFERENCES wallpapers (id),
    verdict      TEXT NOT NULL,
    PRIMARY KEY (batch_id, wallpaper_id)
)
""",
)
"""Migration 2, the **Draft Batch** table.

Keyed `(batch_id, wallpaper_id)`, which enforces one **Verdict** per **Wallpaper** per submission for free.
A separate step rather than an edit to migration 1: that one is already applied to live databases.
"""


_MIGRATION_4 = (
    """
CREATE TABLE library_files (
    wallpaper_id TEXT PRIMARY KEY REFERENCES wallpapers (id),
    path         TEXT NOT NULL,
    written_at   TEXT NOT NULL
)
""",
)
"""Migration 4, the absolute path of every **Library** file written — the deferred decision #4 left here.

Keyed by **Wallpaper**, because the **Library** holds at most one file per **Favourite**. `path` is the
absolute path the writer actually returned, not one derived from the **Library** setting, because that
setting can change and the file does not move with it (invariant 9). `written_at` is display and diagnosis
only; nothing resolves against it, and like every timestamp here it is an ISO 8601 UTC string from the
injected clock (invariant 5).

A row is the one record that a file exists. There is no reading of the folder to check: the **Library** is
write-only, and a file deleted in Explorer is deliberately invisible to wallpapi.
"""


def _migration_3() -> tuple[tuple[str, tuple[str, str]], ...]:
    """Migration 3: seed the settings the settings page edits.

    A function rather than a constant, and statements paired with parameters rather than statements alone,
    because the **Library** default is `Path.home() / "Pictures" / "wallpapi"` — computed here at migration
    time and inserted as text, never interpolated into SQL.

    `DO NOTHING` rather than an upsert: this runs on databases that predate it as well as on empty ones,
    and a database that already holds a chosen `batch_size` must keep it. It also backfills `batch_size`
    into the one shape that could be missing it — a `settings` table hand-edited since migration 1.
    """
    return tuple((_SEED_SETTING, (key, value)) for key, value in _defaults().items())


_SEED_SETTING = "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING"


_CREATE_POOL = """
CREATE TABLE pool (
    wallpaper_id TEXT PRIMARY KEY REFERENCES wallpapers (id),
    fetched_at   TEXT NOT NULL,
    source       TEXT NOT NULL
)
"""
"""**Pool** membership as its own table, never a column on `wallpapers`.

A **Wallpaper** leaves the **Pool** — pruned by a **Filter** change — while the **Decision log** goes on
referring to its `wallpapers` row for ever. (#9 turned out to evict nothing: a **Dud** keeps its place and
is simply not drawn, because a **Score** is derived and the next **Verdict** can make it a **Banger**
again.) An `in_pool` column
would make "is it in the **Pool**" and "does this row exist" the same question, and there would be nowhere
to put `fetched_at` or `source` without widening a table that is about the image itself.

`source` is how it got here: `'random'` today, `'like'` when #13 starts searching for lookalikes.
"""


def _migration_5() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 5: the **Pool** table, and the **Filters** and **Pool** target size as seeded rows.

    Paired with parameters for the same reason migration 3 is — the seeds come from `_defaults()`, which is
    computed in Python and inserted as text rather than interpolated into SQL. `DO NOTHING` again, because
    this also runs on databases that already hold settings chosen by hand.

    A step of its own rather than an edit to 3: that one is already applied to live databases.
    """
    return ((_CREATE_POOL, ()), *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()))


def _migration_6() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 6: the **Zone** a **Batch** showed each **Wallpaper** from, and the two similarity
    settings.

    `zone` is nullable, and deliberately so. **Scores** are never stored (invariant 2) — this column is not
    a **Score** and not a cache of one, it is the record of which **Zone** the **Wallpaper** was drawn from
    at mint time, which is a fact about the **Batch** rather than about the **Wallpaper**. A **Batch**
    minted before this migration has no such fact, so its rows are NULL and its tiles render without a
    label rather than claiming a **Zone** nobody classified them into.

    Paired with parameters like migrations 3 and 5, because the seeds come from `_defaults()` and are
    inserted as text rather than interpolated into SQL. `DO NOTHING` again: this also runs on databases
    that already hold settings chosen by hand.
    """
    return (
        ("ALTER TABLE batch_wallpapers ADD COLUMN zone TEXT", ()),
        *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()),
    )


_CREATE_MIXES = """
CREATE TABLE mixes (
    name    TEXT PRIMARY KEY,
    unknown INTEGER NOT NULL,
    banger  INTEGER NOT NULL,
    dud     INTEGER NOT NULL
)
"""
"""**Mixes** as stored rows, never a constant in the code.

**Explore** and **Refine** are the two the spec names and the only two anything writes today, so a pair of
module constants would do everything this ticket needs. The table is here anyway because #12 makes
**Mixes** editable and lets the user add their own, and the difference between "a constant that #12
replaces with a table" and "a table #12 starts writing to" is a migration against live **Batches** versus
a new endpoint. One `INSERT` now is cheaper than that later.

One column per **Zone** rather than a `zone`/`percentage` row per share. A **Mix** is three numbers that
have to be read together and that mean nothing apart — the sum is the invariant — and a narrow table would
let a **Zone** simply be missing, which is a shape `Mix` cannot express and `validated_mix` would have to
learn to refuse.

No CHECK constraint on the sum. The rule lives in `validated_mix` (and so in one place), it is the same
rule the form at #12 has to state in words anyway, and a row that breaks it is dropped on the way out
rather than making the table unreadable.
"""

_SEED_MIX = """
INSERT INTO mixes (name, unknown, banger, dud) VALUES (?, ?, ?, ?)
ON CONFLICT (name) DO NOTHING
"""
"""`DO NOTHING`, so that a re-run — or a database that already holds an **Explore** the user has edited at
#12 — keeps what is there. A migration must not undo a setting."""


def _migration_7() -> tuple[tuple[str, tuple[str | int, ...]], ...]:
    """Migration 7: **Mixes** as a table, seeded with **Explore** and **Refine**, and the active one as a
    setting.

    Paired with parameters like migrations 3, 5 and 6, because the seeds are computed in Python —
    `DEFAULT_MIXES` here, `_defaults()` for the settings — and inserted as values rather than interpolated
    into SQL.

    The active **Mix** is an ordinary `settings` row and not a column on `mixes`. "Which one is selected"
    is one fact about the whole table, and a flag per row is a fact that can be true twice.
    """
    return (
        (_CREATE_MIXES, ()),
        *((_SEED_MIX, (mix.name, mix.unknown, mix.banger, mix.dud)) for mix in DEFAULT_MIXES),
        *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()),
    )


def _migration_8() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 8: the **Revisit weight** as a seeded `settings` row.

    No table and no column — a setting is a row, and `Settings` is the typed view over those rows (ADR
    0004), so this is a seed and nothing else. `DO NOTHING` like migrations 3, 5, 6 and 7, because it runs
    on databases that already hold settings chosen by hand and a migration must not undo one.

    A step of its own rather than an edit to any of those: they are already applied to live databases.

    Since #38 the setting is gone and `_defaults()` no longer names it, so on a fresh database this seeds
    nothing new, and migration 10 deletes the row from a database that already had it (ADR 0016).
    """
    return tuple((_SEED_SETTING, (key, value)) for key, value in _defaults().items())


_RETUNE_SETTING = "UPDATE settings SET value = ? WHERE key = ? AND value = ?"
"""Change a setting, but only where it still holds the value a previous migration seeded.

The `AND value = ?` is the whole point and is the only safe way to move a default that is already in live
databases: a row the user has edited does not match, so it is left exactly as they set it, and a migration
never silently undoes a choice. The alternative — deleting the row so the next read re-seeds it — would
throw away a tuned value without being able to tell it from an untouched one.
"""


def _migration_9() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 9: the **Similarity radius** default moves from 0.5 to 0.15 (ADR 0013).

    The provider changed, so the number changed with it: image-embedding similarities are bunched into
    the top of the range and 0.5 made every decided **Wallpaper** a neighbour of the whole **Pool**.

    **Only where the row still says 0.5.** A database whose owner has already tuned the radius keeps what
    they chose — that is what `_RETUNE_SETTING`'s third parameter is for — and a database that has never
    had the row seeded at all gets it from the seed below, which is the ordinary `DO NOTHING` insert every
    settings migration since 3 has used. Both halves are needed: the update does nothing on a fresh
    database and the insert does nothing on an old one.
    """
    radius = _defaults()[_SIMILARITY_RADIUS]
    return (
        (_RETUNE_SETTING, (radius, _SIMILARITY_RADIUS, str(SUPERSEDED_SIMILARITY_RADIUS))),
        *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()),
    )


_REVISIT_WEIGHT_KEY = "revisit_weight"
"""The `settings` key migration 8 seeded, kept only so migration 10 can delete the row (ADR 0016)."""


def _migration_10() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Migration 10: decide once (#38, ADR 0016).

    Four things, each the migration half of something the code now does on its own.

    **The Pool gives up everything the Decision log mentions.** The third enforcement point of the standing
    rule, beside `submit_batch` and `_admit_to_pool`: a database from before #38 still holds every
    **Wallpaper** an earlier **Batch** showed. Any entry counts, a legacy `cleared` one included. Only the
    membership row goes — the `wallpapers` row, the log and any **Embedding** stay, and each stays a decided
    column of every **Score** (ADR 0007).

    **The Decision log is indexed by Wallpaper**, because admission now asks it "is this one mentioned?"
    for every **Wallpaper** a search returns. Without the index each question scans a log that only grows.
    Here rather than in a later step because a migration not yet applied anywhere costs nothing to widen.

    **The Revisit weight's row is deleted**, because the setting is gone and a row nothing reads is a
    setting that looks configured and is not.

    **The Pool target moves from 2000 to 500, only where it still says 2000**, on migration 9's precedent:
    a database whose owner chose a number keeps it, and a fresh one gets 500 from the seed. Nothing here
    trims a **Pool** above its new target. It drains — each submission retires what it showed and the
    refill idles until the **Pool** is below target — because trimming would throw away **Wallpapers**
    already fetched, filtered and thumbnailed for the chance of finding them again.
    """
    target = _defaults()[_POOL_TARGET_SIZE]
    return (
        ("DELETE FROM pool WHERE wallpaper_id IN (SELECT wallpaper_id FROM decision_log)", ()),
        ("CREATE INDEX IF NOT EXISTS decision_log_by_wallpaper ON decision_log (wallpaper_id)", ()),
        ("DELETE FROM settings WHERE key = ?", (_REVISIT_WEIGHT_KEY,)),
        (_RETUNE_SETTING, (target, _POOL_TARGET_SIZE, str(SUPERSEDED_POOL_TARGET_SIZE))),
        *((_SEED_SETTING, (key, value)) for key, value in _defaults().items()),
    )
