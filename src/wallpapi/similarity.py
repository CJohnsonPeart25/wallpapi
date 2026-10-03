"""The **Similarity provider** seam, and the baseline provider behind it.

The interface is a matrix, **Pool** x decided, never pairwise (invariant 2): a pairwise call forces a Python
loop and tempts caching **Scores**.
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
"""What a provider with no upkeep returns from `catch_up`, in seconds."""


class SimilarityProvider(Protocol):
    """How alike **Wallpapers** are."""

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        """A `len(pool)` x `len(decided)` matrix of similarities in `[0, 1]`."""
        ...

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Do one step of this provider's upkeep and return how long to wait before the next.

        On the protocol so the Core service never needs to know which provider it holds. Called only from the
        background thread; must never raise, and returns `NOTHING_TO_CATCH_UP` when idle.
        """
        ...

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        """One line for the page when not at full strength, or `None`: a fallback **Score** looks real."""
        ...

    def vectors(self, pool: Sequence[Wallpaper]) -> NDArray[np.float32] | None:
        """Each of `pool`'s positions as a `len(pool)` x d array (a zero row for none), or `None`."""
        ...


HUE_BINS = 12
TONE_BINS = 2
TONE_SPLIT = 0.5
NEUTRAL_BINS = 4
SATURATION_FLOOR = 0.15
CHROMATIC_BINS = HUE_BINS * TONE_BINS

COLOURLESS_BIN = CHROMATIC_BINS + NEUTRAL_BINS
"""A bin for a **Wallpaper** with no usable colours, so its self-cosine stays 1.0."""

BIN_COUNT = COLOURLESS_BIN + 1

CATEGORY_SHARE = 0.25
"""What a matching category is worth, the colours taking the rest: weak evidence."""


class MetadataSimilarityProvider:
    """The baseline: dominant colours and category off the search response. No **API call**.

    Colour cosine over `BIN_COUNT` histogram bins blended with `CATEGORY_SHARE`. Crude, and vectorised: one
    matmul, no loop over pairs.
    """

    def similarities(self, pool: Sequence[Wallpaper], decided: Sequence[Wallpaper]) -> NDArray[np.float32]:
        codes: dict[str, int] = {}
        colour = _histograms(pool) @ _histograms(decided).T
        matching = _category_codes(pool, codes)[:, None] == _category_codes(decided, codes)[None, :]
        similarity = CATEGORY_SHARE * matching + (1.0 - CATEGORY_SHARE) * colour
        # Clipped: float32 rounding can put a self-similarity above 1.0 and make a distance negative.
        return np.clip(similarity, 0.0, 1.0).astype(np.float32)

    def catch_up(self, thumbnails: Path, stop_event: threading.Event) -> float:
        """Nothing to keep up."""
        del thumbnails, stop_event
        return NOTHING_TO_CATCH_UP

    def notice(self, pool: Sequence[Wallpaper]) -> str | None:
        """Never degraded."""
        del pool
        return None

    def vectors(self, pool: Sequence[Wallpaper]) -> NDArray[np.float32] | None:
        """None: a colour histogram is not a position the varied draw was designed on."""
        del pool
        return None


def _category_codes(wallpapers: Sequence[Wallpaper], codes: dict[str, int]) -> NDArray[np.int64]:
    """Each **Wallpaper**'s category as an integer; `codes` is shared across both sides of a matrix."""
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

    counts[counts.sum(axis=1) == 0.0, COLOURLESS_BIN] = 1.0
    return counts / np.linalg.norm(counts, axis=1, keepdims=True)


def _bins(packed: NDArray[np.int64]) -> NDArray[np.int64]:
    """Which bin each packed 24-bit colour falls in, in one numpy pass."""
    red = ((packed >> 16) & 0xFF) / 255.0
    green = ((packed >> 8) & 0xFF) / 255.0
    blue = (packed & 0xFF) / 255.0

    value = np.maximum(np.maximum(red, green), blue)
    chroma = value - np.minimum(np.minimum(red, green), blue)
    # Divisors are guarded: a grey has no hue and black no saturation.
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
    """`"#660000"` as `0x660000`, or `None`: a hand-edited row costs one colour, not every **Score**."""
    text = colour.strip().removeprefix("#")
    if len(text) != 6:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None
