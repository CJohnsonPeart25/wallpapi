"""The **Similarity provider** seam, and the baseline provider behind it.

Invariant 2: the interface is a matrix — **Pool** x decided — and never a pairwise call, because **Scores**
are derived from the **Decision log** on every read and a Python loop over a 10k **Pool** would make caching
them tempting. The matrix is never **Pool** x **Pool**: 10k x 10k is 800MB of float64 and is never needed.

Whole **Wallpapers** on both sides rather than bare IDs. The baseline reads `.colours` and `.category`, and
a provider handed only IDs would have to go back to storage for them — a second seam into the database,
opened by the one dependency that is meant to know nothing about it. The fake still keys its hand-defined
values by `(pool.id, decided.id)`, so nothing about how a test arranges a similarity changes.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from wallpapi.model import Wallpaper

NOTHING_TO_CATCH_UP = 3600.0
"""What a provider with no upkeep of its own returns from `catch_up`, in seconds.

An hour rather than "never", because the thread that calls it has one loop and one way to wait, and a
sentinel would be a second shape for it to understand. A provider that does nothing costs one wake-up an
hour to do nothing in.
"""


class SimilarityProvider(Protocol):
    """How alike **Wallpapers** are. The method is deliberately left open."""

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        """A `len(pool)` x `len(decided)` matrix of similarities, each in `[0, 1]`.

        1.0 means the same **Wallpaper** and 0.0 means nothing in common that this provider can see. The
        **Score** maths turns it into a distance as `1 - similarity` and knows nothing else about how it
        was arrived at.
        """
        ...

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Do one step of whatever upkeep this provider needs, and say how long to wait before the next.

        On the protocol rather than only on the provider that needs it (#14), because the alternative is
        the Core service knowing which provider it is holding — and the whole point of the seam is that it
        does not. A provider with nothing to keep up returns `NOTHING_TO_CATCH_UP` and does nothing, which
        is what the baseline does.

        Called only from the background thread, never from a request: for the embedding provider this is
        an 85MiB download and then a model run per **Wallpaper**, and neither belongs on the path of a
        page load. `thumbnails` is the **Thumbnail cache** directory, because the one image of a
        **Wallpaper** wallpapi already has is the one in there (invariant 8) — a **Wallpaper** whose
        thumbnail has not been fetched yet simply is not in it, so it gets no embedding and every pair it
        is in falls back to the baseline until it has one.

        `stop_event` is the same one the refill waits on. Every wait inside an implementation is
        `stop_event.wait(n)` and every long piece of work checks it between chunks, so shutdown does not
        have to outlast a download (invariant 12). Must never raise: the thread that calls it has nothing
        to catch, for `refill_loop`'s reason.
        """
        ...

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        """One line for the **Batch** page when this provider is not working at full strength, or `None`.

        The page has a seam to exactly one thing (invariant 1), so a provider that is degraded — the
        embedding provider before its model has downloaded, or after the download failed, or while part of
        the **Pool** has no **Embedding** yet — has to be able to say so through the Core service or not at
        all. Silence would be worse than a line: **Scores** computed from the fallback look exactly like
        **Scores** computed properly.

        `pool` is the whole **Pool**, the same shape `similarities` takes (#44), so that a provider can say
        how much of it it covers without the Core service learning which provider it holds. A provider with
        nothing to say about coverage ignores it.

        A sentence for a person, not a reason code. Nothing branches on it.
        """
        ...


HUE_BINS = 12
"""Chromatic colours are binned by hue, 30 degrees to a bin."""

TONE_BINS = 2
"""And split dark from light within each hue, at `TONE_SPLIT`."""

TONE_SPLIT = 0.5
"""The HSV value at which a hue stops counting as dark and starts counting as light."""

NEUTRAL_BINS = 4
"""Anything below `SATURATION_FLOOR` has no meaningful hue and is binned by brightness alone: black, dark
grey, light grey, white."""

SATURATION_FLOOR = 0.15
"""Below this, HSV hue is numerically unstable and visually meaningless — two near-greys a degree apart in
hue are the same colour to anybody looking at them."""

CHROMATIC_BINS = HUE_BINS * TONE_BINS

COLOURLESS_BIN = CHROMATIC_BINS + NEUTRAL_BINS
"""A bin of its own for a **Wallpaper** Wallhaven returned no usable colours for.

Without it such a **Wallpaper** would have an all-zero histogram, whose cosine against anything — itself
included — is undefined. With it, two colourless **Wallpapers** are alike, a colourless one is unlike every
coloured one, and the self-comparison stays 1.0 for every **Wallpaper** there is.
"""

BIN_COUNT = COLOURLESS_BIN + 1

CATEGORY_SHARE = 0.25
"""How much of the similarity a matching category is worth, the colours taking the rest.

A quarter rather than a half: Wallhaven has three categories, so a match is weak evidence — a third of all
**Wallpapers** match any given one by chance — while the colour histogram is what actually tells one image
from another. Non-zero because the categories are not interchangeable: an anime wallpaper and a photograph
of a mountain sharing a palette are still not the same kind of thing.
"""


class MetadataSimilarityProvider:
    """The baseline: dominant colours and category, straight off the search response. No **API call**.

    **The formula.** Each **Wallpaper** becomes a histogram over `BIN_COUNT` colour bins — one per
    hue-and-tone pair for chromatic colours, one per brightness band for near-greys, and one for having no
    colours at all — counted from the hex list Wallhaven's search returns. The histogram is normalised to
    unit length, so colour similarity is the cosine between two of them: 1.0 for identical palettes, 0.0 for
    disjoint ones, and never negative, because no count is. The whole formula is

        similarity = CATEGORY_SHARE * (the categories match) + (1 - CATEGORY_SHARE) * (the colour cosine)

    which is 1.0 for a **Wallpaper** against itself and 0.0 for a different category with no bin in common.

    **It is deliberately crude.** The binning is hard rather than soft, so two nearly identical reds that
    fall either side of a 30-degree boundary contribute nothing to each other, and a palette says nothing
    about composition or subject. That is the price of costing no **API calls** and no model; #14's spike is
    where it stops being the only option. Nothing outside this class may depend on any of it — everything
    downstream sees a number in `[0, 1]`.

    **Vectorised.** No Python loop over pairs anywhere: the matrix is one matmul between two
    `(n, BIN_COUNT)` arrays plus one broadcast comparison of category codes. The only Python iteration is
    over the **Wallpapers**' own colour lists while the histograms are built, which is linear in the
    **Pool** rather than quadratic in it.
    """

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        codes: dict[str, int] = {}
        colour = _histograms(pool) @ _histograms(decided).T
        matching = _category_codes(pool, codes)[:, None] == _category_codes(decided, codes)[None, :]
        similarity = CATEGORY_SHARE * matching + (1.0 - CATEGORY_SHARE) * colour
        # Clipped rather than trusted: the cosine of a unit vector with itself is 1.0 only up to float32
        # rounding, and a similarity of 1.0000001 would make the **Score** maths' distance negative.
        return np.clip(similarity, 0.0, 1.0).astype(np.float32)

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Nothing to keep up. Everything this provider reads is already on the **Wallpaper**."""
        del thumbnails, stop_event
        return NOTHING_TO_CATCH_UP

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        """Never degraded: it has no model to fetch and no cache to fill, so it covers every **Pool**."""
        del pool
        return None


def _category_codes(wallpapers: Sequence[Wallpaper], codes: dict[str, int]) -> NDArray[np.int64]:
    """Each **Wallpaper**'s category as an integer, so "same category" is an integer comparison.

    `codes` is shared between the two sides of one matrix, which is the point of passing it in: the same
    category name has to get the same number on both. Trimmed and lowercased, because the category is a
    string off an API response rather than an enum wallpapi controls.
    """
    return np.array(
        [codes.setdefault(w.category.strip().lower(), len(codes)) for w in wallpapers], dtype=np.int64
    )


def _histograms(wallpapers: Sequence[Wallpaper]) -> NDArray[np.float32]:
    """The `len(wallpapers)` x `BIN_COUNT` matrix of unit-length colour histograms."""
    rows: list[int] = []
    packed: list[int] = []
    for index, wallpaper in enumerate(wallpapers):
        for colour in wallpaper.colours:
            rgb = _rgb(colour)
            if rgb is not None:
                rows.append(index)
                packed.append(rgb)

    counts = np.zeros((len(wallpapers), BIN_COUNT), dtype=np.float32)
    if packed:
        binned = _bins(np.array(packed, dtype=np.int64))
        flat = np.bincount(np.array(rows, dtype=np.int64) * BIN_COUNT + binned, minlength=counts.size)
        counts = flat.reshape(len(wallpapers), BIN_COUNT).astype(np.float32)
    # A **Wallpaper** Wallhaven gave no usable colours for lands wholly in the colourless bin, so its
    # histogram has a length and its cosine against itself is 1.0 like every other one's.
    counts[counts.sum(axis=1) == 0.0, COLOURLESS_BIN] = 1.0
    return counts / np.linalg.norm(counts, axis=1, keepdims=True)


def _bins(packed: NDArray[np.int64]) -> NDArray[np.int64]:
    """Which bin each packed 24-bit colour falls in, all of them in one pass.

    HSV is computed here rather than with `colorsys`, which is scalar: this runs over every colour of every
    **Pool** **Wallpaper**, and wants to be one pass of numpy rather than 50,000 Python calls.
    """
    red = ((packed >> 16) & 0xFF) / 255.0
    green = ((packed >> 8) & 0xFF) / 255.0
    blue = (packed & 0xFF) / 255.0

    value = np.maximum(np.maximum(red, green), blue)
    chroma = value - np.minimum(np.minimum(red, green), blue)
    # The divisors are guarded rather than the results patched up afterwards: a zero-chroma colour is a
    # grey, whose hue means nothing, and a zero-value colour is black, whose saturation means nothing.
    saturation = np.where(value > 0.0, chroma / np.where(value > 0.0, value, 1.0), 0.0)
    safe = np.where(chroma > 0.0, chroma, 1.0)
    hue = (
        np.select(
            [chroma == 0.0, value == red, value == green],
            [0.0, ((green - blue) / safe) % 6.0, ((blue - red) / safe) + 2.0],
            default=((red - green) / safe) + 4.0,
        )
        / 6.0
    )

    hue_bin = np.minimum((hue * HUE_BINS).astype(np.int64), HUE_BINS - 1)
    tone_bin = (value >= TONE_SPLIT).astype(np.int64)
    neutral_bin = CHROMATIC_BINS + np.minimum((value * NEUTRAL_BINS).astype(np.int64), NEUTRAL_BINS - 1)
    return np.where(saturation < SATURATION_FLOOR, neutral_bin, hue_bin * TONE_BINS + tone_bin)


def _rgb(colour: str) -> int | None:
    """`"#660000"` as the integer `0x660000`, or `None` if it is not a colour at all.

    Tolerant rather than raising. The colours come off an API response and are stored as one joined string,
    so a truncated or hand-edited row has to cost that one colour rather than every **Score** on the page.
    """
    text = colour.strip().removeprefix("#")
    if len(text) != 6:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None
